"""Tests for the terminal wizard and ``--check`` (``lovia.web.config``)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Callable

import httpx
import pytest

pytest.importorskip("fastapi")

from lovia.exceptions import UserError  # noqa: E402
from lovia.web.config import (  # noqa: E402
    Connection,
    LoadedConfig,
    ModelProfile,
    WebConfig,
    format_app_summary,
    format_summary,
    interactive_setup,
    mask_key,
    run_check,
    storage,
)


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


def make_loaded(
    config: WebConfig | None = None, *, label: str = storage.USER_CONFIG_LABEL
) -> LoadedConfig:
    cfg = config or WebConfig()
    path = (
        storage.user_config_path()
        if label == storage.USER_CONFIG_LABEL
        else storage.project_config_path()
    )
    return LoadedConfig(cfg, path, label, exists=bool(cfg.models))


def single_profile_config(**fields: object) -> WebConfig:
    profile = {"id": "default", "model": "openai:gpt-5.5", **fields}
    return WebConfig.model_validate({"models": [profile]})


def run_wizard(
    loaded: LoadedConfig,
    *,
    inputs: list[str],
    keys: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
    reconfigure: bool = False,
) -> tuple[Connection, str, Callable[[str], str], Callable[[str], str]]:
    out = io.StringIO()
    input_fn = scripted(inputs)
    getpass_fn = scripted(keys or [])
    result = interactive_setup(
        loaded,
        input_fn=input_fn,
        getpass_fn=getpass_fn,
        transport=transport or ok_transport(),
        out=out,
        reconfigure=reconfigure,
    )
    return result, out.getvalue(), input_fn, getpass_fn


# ------------------------------------------------------------- first run -


def test_first_run_asks_everything_and_saves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    loaded = make_loaded()
    conn, output, input_fn, getpass_fn = run_wizard(
        loaded,
        inputs=[
            "deepseek-v4-pro",  # model
            "https://api.deepseek.example",  # base URL (over the shown default)
            "128000",  # context window (unknown model on a custom host)
            "",  # save? -> default yes
        ],
        keys=["sk-deep"],
    )
    assert conn.model == "deepseek-v4-pro"
    assert conn.base_url == "https://api.deepseek.example"
    assert conn.api_key == "sk-deep"
    assert conn.context_window == 128000
    assert "✓ endpoint reachable" in output
    assert "change anytime: lovia web --setup" in output
    assert not input_fn.remaining and not getpass_fn.remaining  # type: ignore[attr-defined]

    # First-run saves land in the user scope: one setup, every directory.
    path = storage.user_config_path()
    assert path.is_file()
    assert not storage.project_config_path().exists()
    assert loaded.exists and loaded.label == storage.USER_CONFIG_LABEL
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {
        "version": 1,
        "models": [
            {
                "id": "deepseek-v4-pro",
                "model": "deepseek-v4-pro",
                "flavor": "openai",
                "base_url": "https://api.deepseek.example",
                "api_key": "sk-deep",
                "context_window": 128000,
                "vision": "auto",
            }
        ],
        "roles": {"chat": "deepseek-v4-pro"},
        "search": {"backend": "auto"},
    }
    # Secrets are protected structurally: owner-only file + a `*` .gitignore
    # over the whole .lovia/ dir (which also holds the chat DB).
    assert (path.parent / ".gitignore").read_text().strip().endswith("*")
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_wizard_asks_only_whats_missing() -> None:
    """A hand-written config missing only the key asks only the key question."""
    cfg = WebConfig.model_validate(
        {
            "models": [
                {"id": "d", "model": "openai:custom-model", "base_url": "http://gw/v1"}
            ]
        }
    )
    result, output, input_fn, _ = run_wizard(
        make_loaded(cfg),
        # Exactly two prompts — window and save — or scripted() raises: the
        # configured model and base URL are not re-asked.
        inputs=["", "n"],
        keys=[""],
    )
    assert result.api_key is None
    assert result.context_window is None
    assert not input_fn.remaining  # type: ignore[attr-defined]
    assert "could not verify" not in output


def test_enter_accepts_default_base_url(fake_home: Path) -> None:
    result, _, _, _ = run_wizard(
        make_loaded(),
        inputs=["openai:gpt-5.5", "", "n"],  # model; default base URL; decline save
        keys=["sk-official"],
    )
    assert result.base_url == "https://api.openai.com/v1"
    # gpt-5.5 is in the provider table -> no context-window question.
    assert result.context_window is None


def test_official_host_requires_nonempty_key(fake_home: Path) -> None:
    result, output, _, getpass_fn = run_wizard(
        make_loaded(single_profile_config()),
        inputs=["n"],
        keys=["", "", "sk-finally"],  # two empty attempts, then a real key
    )
    assert result.api_key == "sk-finally"
    assert "required" in output
    assert not getpass_fn.remaining  # type: ignore[attr-defined]


def test_context_window_reprompts_on_garbage() -> None:
    cfg = single_profile_config(model="openai:mystery-model", api_key="sk-x")
    result, output, *_ = run_wizard(
        make_loaded(cfg), inputs=["abc", "-3", "42000", "n"]
    )
    assert result.context_window == 42000
    assert output.count("invalid integer") == 1
    assert "must be >= 1" in output


def test_auth_failure_reprompts_key_until_valid() -> None:
    attempts: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("authorization"))
        return httpx.Response(401 if len(attempts) == 1 else 200)

    result, output, *_ = run_wizard(
        make_loaded(single_profile_config()),
        inputs=["n"],
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

    cfg = WebConfig.model_validate(
        {"models": [{"id": "d", "model": "m", "base_url": "http://typo.example/v1"}]}
    )
    result, output, *_ = run_wizard(
        make_loaded(cfg),
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
    cfg = WebConfig.model_validate(
        {"models": [{"id": "d", "model": "m", "base_url": "http://gw/v1"}]}
    )
    result, output, *_ = run_wizard(
        make_loaded(cfg), inputs=["", "n"], keys=[""], transport=transport
    )
    assert "could not verify" in output
    assert result.base_url == "http://gw/v1"


def test_eof_mid_prompt_raises_user_error() -> None:
    def eof_input(prompt: str) -> str:
        raise EOFError

    with pytest.raises(UserError, match="stdin closed"):
        interactive_setup(
            make_loaded(),
            input_fn=eof_input,
            getpass_fn=scripted([]),
            transport=ok_transport(),
        )


def test_reported_window_serves_the_launch_but_is_not_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    """A deployment fact belongs in the run, not frozen into config.json."""
    monkeypatch.chdir(tmp_path)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"data": [{"id": "deepseek-v4-pro", "max_model_len": 32_768}]}
        )
    )
    conn, output, *_ = run_wizard(
        make_loaded(),
        # model; custom base; save (no window question — the endpoint answered)
        inputs=["deepseek-v4-pro", "http://gw/v1", ""],
        keys=[""],
        transport=transport,
    )
    assert conn.context_window == 32_768
    assert conn.window_from_endpoint is True
    saved = json.loads(storage.user_config_path().read_text(encoding="utf-8"))
    assert "context_window" not in saved["models"][0]


def test_decline_save_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    loaded = make_loaded()
    result, _, _, _ = run_wizard(
        loaded,
        inputs=["some-model", "http://gw/v1", "", "n"],
        keys=[""],
    )
    assert not storage.project_config_path().exists()
    assert not storage.user_config_path().exists()
    # The session still runs on the entered connection, unsaved.
    assert not loaded.exists
    assert loaded.config.default_profile() is not None
    assert result.model == "some-model"


# ---------------------------------------------------------- reconfigure -


def test_reconfigure_enter_keeps_everything() -> None:
    cfg = single_profile_config(api_key="sk-abcdefghijkl1234")
    result, output, input_fn, getpass_fn = run_wizard(
        make_loaded(cfg), inputs=["", "", "n"], keys=[""], reconfigure=True
    )
    assert result.model == "openai:gpt-5.5"
    assert result.base_url == "https://api.openai.com/v1"
    assert result.api_key == "sk-abcdefghijkl1234"
    assert "Enter keeps the current value" in output
    # --setup users already know the entry point; no first-run hint.
    assert "change anytime" not in output
    assert not input_fn.remaining and not getpass_fn.remaining  # type: ignore[attr-defined]


def test_reconfigure_model_change_resets_window_and_saves_user_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = single_profile_config(api_key="sk-abcdefghijkl1234", context_window=100_000)
    result, _, *_ = run_wizard(
        make_loaded(cfg),
        # new model (same flavor), keep base URL, re-enter the reset window,
        # accept the save (default scope: user).
        inputs=["openai:mystery-model", "", "128000", ""],
        keys=[""],
        reconfigure=True,
    )
    assert result.context_window == 128_000  # the stale window did not survive
    saved = json.loads(storage.user_config_path().read_text(encoding="utf-8"))
    # The whole connection is saved as one unit under the existing profile id;
    # the flavor's default base URL is not pinned.
    assert saved["models"] == [
        {
            "id": "default",
            "model": "openai:mystery-model",
            "flavor": "openai",
            "api_key": "sk-abcdefghijkl1234",
            "context_window": 128000,
            "vision": "auto",
        }
    ]
    assert saved["roles"]["chat"] == "default"
    assert not storage.project_config_path().exists()


def test_reconfigure_flavor_change_resets_endpoint_and_key() -> None:
    cfg = single_profile_config(api_key="sk-abcdefghijkl1234")
    result, _, *_ = run_wizard(
        make_loaded(cfg),
        # anthropic model: old endpoint/key are meaningless — the base URL
        # falls back to the new flavor's default and the key is re-asked
        # (required on the official host).
        inputs=["anthropic:claude-sonnet-4-5", "", "n"],
        keys=["sk-ant-secret1234"],
        reconfigure=True,
    )
    assert result.base_url == "https://api.anthropic.com/v1"
    assert result.api_key == "sk-ant-secret1234"


def test_reconfigure_save_defaults_to_the_active_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = single_profile_config(api_key="sk-abcdefghijkl1234")
    run_wizard(
        make_loaded(cfg, label=storage.PROJECT_CONFIG_LABEL),
        inputs=["", "", ""],  # keep everything; Enter takes the default scope
        keys=[""],
        reconfigure=True,
    )
    # The configuration lives in the project file, so that is the default
    # target — saving user-level under it would look like a no-op.
    assert storage.project_config_path().is_file()
    assert not storage.user_config_path().exists()


def test_user_save_warns_when_a_project_file_shadows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    storage.save_config(
        WebConfig(models=[ModelProfile(id="p", model="project-model")]),
        storage.project_config_path(),
    )
    cfg = single_profile_config(api_key="sk-abcdefghijkl1234")
    _, output, *_ = run_wizard(
        make_loaded(cfg),
        inputs=["", "", "u"],  # keep everything; save to the user scope
        keys=[""],
        reconfigure=True,
    )
    assert "wins in this directory" in output


# ------------------------------------------------------------- run_check -


def _conn(model: str = "openai:gpt-5.5", **overrides: object) -> Connection:
    conn = Connection.from_profile(ModelProfile(id="c", model=model))
    for key, value in overrides.items():
        setattr(conn, key, value)
    return conn


def _listing_transport(*ids: str) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": i} for i in ids]})
    )


def test_run_check_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    rc = run_check(
        _conn(api_key="sk-abcdefghijkl1234"),
        version="1.0",
        out=out,
        transport=_listing_transport("gpt-5.5"),
    )
    assert rc == 0
    text = out.getvalue()
    assert "configuration check" in text
    assert "✓ endpoint reachable" in text
    assert "config files" in text
    assert "./.lovia/config.json (absent)" in text
    assert "~/.lovia/config.json (absent)" in text


def test_run_check_auth_failure_is_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    rc = run_check(
        _conn(api_key="sk-bad"),
        version="1.0",
        out=out,
        transport=httpx.MockTransport(lambda request: httpx.Response(401)),
    )
    assert rc == 2
    assert "✗ authentication failed" in out.getvalue()


def test_run_check_missing_config_is_exit_2_without_probing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    rc = run_check(Connection(), version="1.0", out=out)
    assert rc == 2
    text = out.getvalue()
    assert "missing: model" in text
    assert "--setup" in text  # the recovery hint rides along


# ---------------------------------------------------------------- summary -


def test_mask_key() -> None:
    assert mask_key(None) == "(none)"
    assert mask_key("short") == "…"
    assert mask_key("sk-abcdefghijkl1234") == "sk-…1234"


def test_format_summary_shows_values_and_config() -> None:
    conn = _conn(api_key="sk-abcdefghijkl1234")
    text = format_summary(
        conn,
        version="0.9.0",
        url="http://127.0.0.1:8000",
        config_desc="~/.lovia/config.json",
        workspace_desc="/work (trusted)",
        db_desc="lovia.db",
    )
    assert "lovia v0.9.0" in text
    assert "openai:gpt-5.5" in text
    assert "sk-…1234" in text
    assert "~/.lovia/config.json" in text
    # gpt-5.5 is in the provider's static table.
    assert "auto (provider reports" in text
    assert text.endswith("serving on http://127.0.0.1:8000")


def test_format_summary_keyless_gateway() -> None:
    conn = Connection.from_profile(
        ModelProfile(id="g", model="mystery", base_url="http://gw/v1")
    )
    text = format_summary(
        conn,
        version="0.9.0",
        url="http://127.0.0.1:9000",
        config_desc="(not saved — session only)",
        workspace_desc="(none)",
        db_desc="x.db",
    )
    assert "(none — endpoint does not require one)" in text
    assert "auto (reactive overflow handling)" in text


def test_summary_names_the_env_key_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key left out of the config but present in the provider env is said."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    conn = _conn()  # no api_key stored
    text = format_summary(
        conn,
        version="0.9.0",
        url="http://127.0.0.1:8000",
        config_desc="~/.lovia/config.json",
        workspace_desc="(none)",
        db_desc="x.db",
    )
    assert "OPENAI_API_KEY from the environment applies" in text


def test_format_app_summary() -> None:
    text = format_app_summary(
        version="0.9.0",
        app_target="myagents:assistant",
        db_desc="x.db",
        url="http://127.0.0.1:8000",
    )
    assert "myagents:assistant" in text
    assert text.endswith("serving on http://127.0.0.1:8000")
