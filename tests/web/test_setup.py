"""Tests for the web CLI onboarding module (``lovia.web.setup``)."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Callable

import httpx
import pytest

pytest.importorskip("fastapi")

from lovia.exceptions import UserError  # noqa: E402
from lovia.web import setup  # noqa: E402


def scripted(answers: list[str]) -> Callable[[str], str]:
    """An input()/getpass() stand-in that pops pre-baked answers."""
    remaining = list(answers)

    def _fn(prompt: str) -> str:
        assert remaining, f"unexpected extra prompt: {prompt!r}"
        return remaining.pop(0)

    _fn.remaining = remaining  # type: ignore[attr-defined]
    return _fn


def ok_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))


# ----------------------------------------------------------------- flavor -


@pytest.mark.parametrize(
    ("spec", "name"),
    [
        ("openai:gpt-5.5", "openai"),
        ("oai:x", "openai"),
        ("openai-chat:x", "openai"),
        ("anthropic:claude-sonnet-4-5", "anthropic"),
        ("claude:claude-sonnet-4-5", "anthropic"),
        ("deepseek-v4-pro", "openai"),
        ("somevendor:model", "openai"),
    ],
)
def test_flavor_for_model_mirrors_provider_routing(spec: str, name: str) -> None:
    assert setup.flavor_for_model(spec).name == name


def test_flavors_reuse_provider_constants() -> None:
    assert setup.OPENAI_FLAVOR.default_base_url == "https://api.openai.com/v1"
    assert setup.ANTHROPIC_FLAVOR.default_base_url == "https://api.anthropic.com/v1"


def test_auth_headers_openai_and_anthropic() -> None:
    assert setup.OPENAI_FLAVOR.auth_headers("sk-1") == {"Authorization": "Bearer sk-1"}
    assert setup.OPENAI_FLAVOR.auth_headers(None) == {}
    anthropic = setup.ANTHROPIC_FLAVOR.auth_headers("sk-2")
    assert anthropic["x-api-key"] == "sk-2"
    assert "anthropic-version" in anthropic
    assert "x-api-key" not in setup.ANTHROPIC_FLAVOR.auth_headers(None)


# ----------------------------------------------------- resolve_connection -


def _resolve(**overrides: object) -> setup.Connection:
    kwargs: dict = dict(
        model_flag=None,
        base_url_flag=None,
        api_key_flag=None,
        context_window_flag=None,
        env_sources={},
    )
    kwargs.update(overrides)
    return setup.resolve_connection(**kwargs)


def test_flags_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    conn = _resolve(
        model_flag="openai:m", base_url_flag="http://flag/v1/", api_key_flag="sk-flag"
    )
    assert (conn.base_url, conn.base_url_source) == ("http://flag/v1", "flag")
    assert (conn.api_key, conn.api_key_source) == ("sk-flag", "flag")
    assert (conn.model, conn.model_source) == ("openai:m", "flag")


def test_env_values_and_source_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_MODEL", "anthropic:claude-x")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gw/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    conn = _resolve(env_sources={"ANTHROPIC_BASE_URL": ".env", "LOVIA_MODEL": "config"})
    assert (conn.model, conn.model_source) == ("anthropic:claude-x", "config")
    assert (conn.base_url, conn.base_url_source) == ("http://gw/anthropic", ".env")
    # Not present in env_sources -> plain process env.
    assert (conn.api_key, conn.api_key_source) == ("sk-a", "env")


def test_lovia_scoped_endpoint_env_names_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision: no LOVIA_BASE_URL/LOVIA_API_KEY twins — only provider names."""
    monkeypatch.setenv("LOVIA_BASE_URL", "http://nope")
    monkeypatch.setenv("LOVIA_API_KEY", "sk-nope")
    conn = _resolve(model_flag="openai:m")
    assert conn.base_url == "https://api.openai.com/v1"
    assert conn.api_key is None


def test_default_base_url_is_the_official_endpoint() -> None:
    conn = _resolve(model_flag="anthropic:claude-x")
    assert conn.base_url == "https://api.anthropic.com/v1"
    assert conn.base_url_source == "default"


def test_context_window_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_CONTEXT_WINDOW", "9000")
    conn = _resolve(model_flag="openai:m", env_sources={"LOVIA_CONTEXT_WINDOW": ".env"})
    assert (conn.context_window, conn.context_window_source) == (9000, ".env")


def test_context_window_env_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_CONTEXT_WINDOW", "lots")
    with pytest.raises(UserError, match="invalid integer"):
        _resolve(model_flag="openai:m")


def test_context_window_flag_rejects_non_positive() -> None:
    with pytest.raises(UserError, match="must be >= 1"):
        _resolve(model_flag="openai:m", context_window_flag=0)


def test_missing_model() -> None:
    assert _resolve().missing() == ["model"]


def test_missing_key_on_official_host() -> None:
    assert _resolve(model_flag="openai:gpt-5.5").missing() == ["API key"]


def test_keyless_gateway_is_complete() -> None:
    conn = _resolve(
        model_flag="deepseek-v4-pro", base_url_flag="http://localhost:11434/v1"
    )
    assert conn.missing() == []


def test_official_host_with_key_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert _resolve(model_flag="openai:gpt-5.5").missing() == []


# ------------------------------------------------------------- validation -


def _conn(model: str = "deepseek-v4-pro", **overrides: object) -> setup.Connection:
    conn = setup.Connection(model=model, model_source="flag")
    setup._derive_endpoint(conn, {})
    for key, value in overrides.items():
        setattr(conn, key, value)
    return conn


def test_validate_ok_and_openai_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    conn = _conn(base_url="http://gw/v1", api_key="sk-1")
    outcome, _ = setup.validate_connection(conn, transport=httpx.MockTransport(handler))
    assert outcome is setup.ValidationOutcome.OK
    assert str(seen[0].url) == "http://gw/v1/models"
    assert seen[0].headers["authorization"] == "Bearer sk-1"


def test_validate_anthropic_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    conn = _conn(model="anthropic:claude-x", base_url="http://gw/v1", api_key="sk-2")
    setup.validate_connection(conn, transport=httpx.MockTransport(handler))
    assert seen[0].headers["x-api-key"] == "sk-2"
    assert "anthropic-version" in seen[0].headers


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (401, setup.ValidationOutcome.AUTH_FAILED),
        (403, setup.ValidationOutcome.AUTH_FAILED),
        (404, setup.ValidationOutcome.UNVERIFIABLE),
        (500, setup.ValidationOutcome.UNVERIFIABLE),
    ],
)
def test_validate_status_classification(
    status: int, outcome: setup.ValidationOutcome
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    got, detail = setup.validate_connection(_conn(), transport=transport)
    assert got is outcome
    assert str(status) in detail


def test_validate_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns boom")

    got, detail = setup.validate_connection(
        _conn(), transport=httpx.MockTransport(handler)
    )
    assert got is setup.ValidationOutcome.UNREACHABLE
    assert "dns boom" in detail


# ------------------------------------- context window reported by /models -


def _models_transport(*entries: dict) -> httpx.MockTransport:
    payload = {"object": "list", "data": list(entries)}
    return httpx.MockTransport(lambda request: httpx.Response(200, json=payload))


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"id": "deepseek-v4-pro", "max_model_len": 32_768}, 32_768),  # vLLM/SGLang
        ({"id": "deepseek-v4-pro", "context_window": 131_072}, 131_072),  # Groq
        ({"id": "deepseek-v4-pro", "context_length": 8_192}, 8_192),  # Together
        (
            {  # OpenRouter: the routed provider's limit beats the model-level one
                "id": "deepseek-v4-pro",
                "context_length": 1_000_000,
                "top_provider": {"context_length": 64_000},
            },
            64_000,
        ),
    ],
)
def test_validate_adopts_the_window_the_endpoint_reports(
    entry: dict, expected: int
) -> None:
    conn = _conn(base_url="http://gw/v1", api_key="sk-1")
    outcome, _ = setup.validate_connection(conn, transport=_models_transport(entry))
    assert outcome is setup.ValidationOutcome.OK
    assert (conn.context_window, conn.context_window_source) == (expected, "endpoint")


@pytest.mark.parametrize(
    "transport",
    [
        # The official OpenAI/Anthropic/DeepSeek shape publishes no window.
        _models_transport({"id": "deepseek-v4-pro", "owned_by": "deepseek"}),
        _models_transport({"id": "some-other-model", "max_model_len": 4096}),
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json")),
        httpx.MockTransport(lambda request: httpx.Response(200, json={"data": "nope"})),
    ],
)
def test_validate_leaves_the_window_unset_when_unreported(
    transport: httpx.MockTransport,
) -> None:
    conn = _conn(base_url="http://gw/v1", api_key="sk-1")
    outcome, _ = setup.validate_connection(conn, transport=transport)
    assert outcome is setup.ValidationOutcome.OK
    assert conn.context_window is None


def test_validate_never_overrides_a_configured_window() -> None:
    conn = _conn(base_url="http://gw/v1", api_key="sk-1")
    conn.context_window, conn.context_window_source = 111_111, "flag"
    setup.validate_connection(
        conn,
        transport=_models_transport({"id": "deepseek-v4-pro", "max_model_len": 4096}),
    )
    assert (conn.context_window, conn.context_window_source) == (111_111, "flag")


def test_reported_window_skips_the_prompt_and_is_not_persisted() -> None:
    """A deployment fact belongs in the run, not frozen into ./.env."""
    conn = _conn(base_url="http://gw/v1", api_key="sk-1")
    setup.validate_connection(
        conn,
        transport=_models_transport({"id": "deepseek-v4-pro", "max_model_len": 32_768}),
    )

    def refuse(prompt: str) -> str:  # pragma: no cover - must never be called
        raise AssertionError(f"asked for a window it already knows: {prompt!r}")

    setup._maybe_prompt_context_window(conn, input_fn=refuse, out=io.StringIO())
    assert conn.context_window == 32_768
    assert "32,768 (endpoint)" in setup._context_window_cell(conn)

    # Nothing was entered by hand, so there is nothing to persist: an
    # "endpoint" window must never reach ./.env, where it would go on
    # lying after the deployment is resized. With an empty save set
    # ``_offer_to_save`` returns before it can prompt.
    conn.model_source = conn.base_url_source = conn.api_key_source = "flag"
    setup._offer_to_save(conn, input_fn=refuse, out=io.StringIO())


# ------------------------------------------------------------- the wizard -


def run_wizard(
    conn: setup.Connection,
    *,
    inputs: list[str],
    keys: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
    env_sources: dict[str, str] | None = None,
) -> tuple[setup.Connection, str, Callable[[str], str], Callable[[str], str]]:
    out = io.StringIO()
    input_fn = scripted(inputs)
    getpass_fn = scripted(keys or [])
    result = setup.interactive_setup(
        conn,
        env_sources=env_sources or {},
        input_fn=input_fn,
        getpass_fn=getpass_fn,
        transport=transport or ok_transport(),
        out=out,
    )
    return result, out.getvalue(), input_fn, getpass_fn


def test_first_run_asks_everything_and_saves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    conn, output, input_fn, getpass_fn = run_wizard(
        setup.Connection(),
        inputs=[
            "deepseek-v4-pro",  # model
            "https://api.deepseek.example",  # base URL (over the shown default)
            "128000",  # context window (unknown model)
            "",  # save? -> default yes
        ],
        keys=["sk-deep"],
    )
    assert (conn.model, conn.model_source) == ("deepseek-v4-pro", "prompt")
    assert conn.base_url == "https://api.deepseek.example"
    assert conn.base_url_source == "prompt"
    assert (conn.api_key, conn.api_key_source) == ("sk-deep", "prompt")
    assert (conn.context_window, conn.context_window_source) == (128000, "prompt")
    assert "✓ endpoint reachable" in output
    assert not input_fn.remaining and not getpass_fn.remaining  # type: ignore[attr-defined]

    path = setup.config_path()
    assert path.is_file()
    from dotenv import dotenv_values

    saved = dotenv_values(path)
    assert saved == {
        "LOVIA_MODEL": "deepseek-v4-pro",
        "OPENAI_BASE_URL": "https://api.deepseek.example",
        "OPENAI_API_KEY": "sk-deep",
        "LOVIA_CONTEXT_WINDOW": "128000",
    }
    # Secrets are protected structurally: owner-only file + a `*` .gitignore
    # over the whole .lovia/ dir (which also holds the chat DB).
    assert (path.parent / ".gitignore").read_text().strip().endswith("*")
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_wizard_asks_only_whats_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model+base_url from env: only the key question (and no save offer for env values)."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gw/v1")
    conn = _resolve(model_flag="openai:custom-model")
    # base_url came from env -> no base URL question; key skippable on a gateway;
    # custom model unknown to the table -> context window question appears.
    result, output, *_ = run_wizard(conn, inputs=["", "n"], keys=[""])
    assert result.api_key is None
    assert result.context_window is None
    assert "could not verify" not in output


def test_enter_accepts_default_base_url() -> None:
    conn = _resolve(model_flag="openai:gpt-5.5")
    result, _, _, _ = run_wizard(
        conn,
        inputs=["", "n"],  # accept default base URL; decline save
        keys=["sk-official"],
    )
    assert result.base_url == "https://api.openai.com/v1"
    assert result.base_url_source == "prompt"
    # gpt-5.5 is in the provider table -> no context-window question.
    assert result.context_window is None


def test_official_host_requires_nonempty_key() -> None:
    conn = _resolve(model_flag="openai:gpt-5.5")
    result, output, _, getpass_fn = run_wizard(
        conn,
        inputs=["", "n"],
        keys=["", "", "sk-finally"],  # two empty attempts, then a real key
    )
    assert result.api_key == "sk-finally"
    assert "required" in output
    assert not getpass_fn.remaining  # type: ignore[attr-defined]


def test_context_window_reprompts_on_garbage() -> None:
    conn = _resolve(model_flag="openai:mystery-model", api_key_flag="sk-x")
    result, output, *_ = run_wizard(conn, inputs=["", "abc", "-3", "42000", "n"])
    assert result.context_window == 42000
    assert output.count("invalid integer") == 1
    assert "must be >= 1" in output


def test_auth_failure_reprompts_key_until_valid() -> None:
    attempts: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("authorization"))
        return httpx.Response(401 if len(attempts) == 1 else 200)

    conn = _resolve(model_flag="openai:gpt-5.5")
    result, output, *_ = run_wizard(
        conn,
        inputs=["", "n"],
        keys=["sk-bad", "sk-good"],
        transport=httpx.MockTransport(handler),
    )
    assert result.api_key == "sk-good"
    assert attempts == ["Bearer sk-bad", "Bearer sk-good"]
    assert "authentication failed" in output


def test_unreachable_reprompts_base_url() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "typo.example":
            raise httpx.ConnectError("no such host")
        return httpx.Response(200)

    conn = _resolve(model_flag="m", base_url_flag="http://typo.example/v1")
    result, output, *_ = run_wizard(
        conn,
        # corrected base URL; Enter for the (unknown model) context window; no save
        inputs=["http://right.example/v1", "", "n"],
        keys=[""],
        transport=httpx.MockTransport(handler),
    )
    assert result.base_url == "http://right.example/v1"
    assert hosts == ["typo.example", "right.example"]
    assert "cannot reach" in output


def test_unverifiable_endpoint_continues_with_note() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    conn = _resolve(model_flag="m", base_url_flag="http://gw/v1")
    result, output, *_ = run_wizard(conn, inputs=["", "n"], keys=[""])
    del result
    conn2 = _resolve(model_flag="m", base_url_flag="http://gw/v1")
    result2, output2, *_ = run_wizard(
        conn2, inputs=["", "n"], keys=[""], transport=transport
    )
    assert "could not verify" in output2
    assert result2.base_url == "http://gw/v1"


def test_eof_mid_prompt_raises_user_error() -> None:
    def eof_input(prompt: str) -> str:
        raise EOFError

    with pytest.raises(UserError, match="stdin closed"):
        setup.interactive_setup(
            setup.Connection(),
            env_sources={},
            input_fn=eof_input,
            getpass_fn=scripted([]),
            transport=ok_transport(),
        )


def test_save_only_persists_prompt_sourced_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    conn = _resolve(model_flag="openai:mystery-model")
    # base URL accepted at prompt, context window entered; key/model not prompted.
    result, output, _, _ = run_wizard(conn, inputs=["", "77000", ""])
    del result
    from dotenv import dotenv_values

    saved = dotenv_values(setup.config_path())
    assert "OPENAI_API_KEY" not in saved
    assert "LOVIA_MODEL" not in saved
    assert saved["OPENAI_BASE_URL"] == "https://api.openai.com/v1"
    assert saved["LOVIA_CONTEXT_WINDOW"] == "77000"
    # The .lovia/ dir is git-ignored regardless of which values were saved.
    assert (setup.config_path().parent / ".gitignore").is_file()


def test_decline_save_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    result, _, _, _ = run_wizard(
        setup.Connection(),
        inputs=["some-model", "http://gw/v1", "", "n"],
        keys=[""],
    )
    del result
    assert not setup.config_path().exists()


# ------------------------------------------------------------ persistence -


def test_save_env_file_creates_the_file(tmp_path: Path) -> None:
    path = setup.save_env_file(
        {"A_KEY": "value", "B_KEY": "x y"}, path=tmp_path / ".env"
    )
    assert path.read_text() == "A_KEY=value\nB_KEY=x y\n"
    if os.name == "posix":  # secrets → owner-only
        assert path.stat().st_mode & 0o777 == 0o600


def test_save_env_file_default_path_is_protected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No explicit path -> .lovia/config.env, owner-only, under a git-ignored dir."""
    monkeypatch.chdir(tmp_path)
    path = setup.save_env_file({"OPENAI_API_KEY": "sk-secret"})
    assert path == setup.config_path()
    assert path.read_text() == "OPENAI_API_KEY=sk-secret\n"
    assert (path.parent / ".gitignore").read_text().strip().splitlines()[-1] == "*"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600  # secret file: owner-only
        assert path.parent.stat().st_mode & 0o777 == 0o700  # dir: owner-only too


def test_save_env_file_appends_and_patches_missing_newline(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# my note\nOTHER_KEY=1")  # no trailing newline
    setup.save_env_file({"OPENAI_API_KEY": "sk-new"}, path=path)
    assert path.read_text() == "# my note\nOTHER_KEY=1\nOPENAI_API_KEY=sk-new\n"


def test_save_env_file_appended_duplicate_wins(tmp_path: Path) -> None:
    # Append-only by design: python-dotenv's last-occurrence-wins parsing
    # makes the newer value effective without any rewrite machinery.
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=old\n")
    setup.save_env_file({"OPENAI_API_KEY": "new"}, path=path)
    from dotenv import dotenv_values

    assert dotenv_values(path) == {"OPENAI_API_KEY": "new"}


# ---------------------------------------------------------------- summary -


def test_mask_key() -> None:
    assert setup.mask_key(None) == "(none)"
    assert setup.mask_key("short") == "…"
    assert setup.mask_key("sk-abcdefghijkl1234") == "sk-…1234"


def test_format_summary_shows_values_and_sources() -> None:
    conn = _conn(
        model="openai:gpt-5.5",
        base_url="https://api.openai.com/v1",
        api_key="sk-abcdefghijkl1234",
        api_key_source="config",
    )
    text = setup.format_summary(
        conn,
        version="0.9.0",
        url="http://127.0.0.1:8000",
        workspace_desc="/work (trusted)",
        db_desc="lovia.db",
    )
    assert "lovia v0.9.0" in text
    assert "openai:gpt-5.5 (flag)" in text
    assert "sk-…1234 (config)" in text
    # gpt-5.5 is in the provider's static table.
    assert "auto (provider reports" in text
    assert text.endswith("serving on http://127.0.0.1:8000")


def test_format_summary_keyless_gateway() -> None:
    conn = _conn(model="mystery", base_url="http://gw/v1")
    text = setup.format_summary(
        conn,
        version="0.9.0",
        url="http://127.0.0.1:9000",
        workspace_desc="(none)",
        db_desc="x.db",
    )
    assert "(none — endpoint does not require one)" in text
    assert "auto (reactive overflow handling)" in text


def test_format_app_summary() -> None:
    text = setup.format_app_summary(
        version="0.9.0",
        app_target="myagents:assistant",
        db_desc="x.db",
        url="http://127.0.0.1:8000",
    )
    assert "myagents:assistant" in text
    assert text.endswith("serving on http://127.0.0.1:8000")
