"""Tests for the ``python -m lovia.web`` command-line launcher."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from lovia import Agent, Memory, RetryPolicy  # noqa: E402
from lovia.context import Compaction  # noqa: E402
from lovia.exceptions import UserError  # noqa: E402
from lovia.web import ChatStore  # noqa: E402
from lovia.web import __main__ as cli  # noqa: E402


# ---------------------------------------------------------------- skills -


def test_resolve_skills_explicit_dirs(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert cli.resolve_skills_dirs([str(a), str(b)]) == [a, b]


def test_resolve_skills_explicit_missing_errors(tmp_path: Path) -> None:
    with pytest.raises(UserError, match="skills directory not found"):
        cli.resolve_skills_dirs([str(tmp_path / "nope")])


def test_resolve_skills_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    d = tmp_path / "team"
    d.mkdir()
    monkeypatch.setenv("LOVIA_SKILLS_DIR", str(d))
    assert cli.resolve_skills_dirs(None) == [d]


def test_resolve_skills_default_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LOVIA_SKILLS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skills").mkdir()
    assert cli.resolve_skills_dirs(None) == [Path("skills")]


def test_resolve_skills_default_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LOVIA_SKILLS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.resolve_skills_dirs(None) == []


# ----------------------------------------------------------------- memory -


def test_resolve_memory_disabled() -> None:
    assert cli.resolve_memory("./anywhere", no_memory=True) is None


def test_resolve_memory_default_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LOVIA_MEMORY_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    mem = cli.resolve_memory(None, no_memory=False)
    assert isinstance(mem, Memory)
    assert mem.index is not None  # default builds both the notes and archive tiers
    # The default root is created eagerly under cwd.
    assert (tmp_path / ".lovia" / "memory").is_dir()


def test_resolve_memory_explicit_dir(tmp_path: Path) -> None:
    target = tmp_path / "mem"
    mem = cli.resolve_memory(str(target), no_memory=False)
    assert isinstance(mem, Memory)
    assert target.is_dir()


def test_resolve_memory_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    envmem = tmp_path / "envmem"
    monkeypatch.setenv("LOVIA_MEMORY_DIR", str(envmem))
    mem = cli.resolve_memory(None, no_memory=False)
    assert isinstance(mem, Memory)
    assert envmem.is_dir()


def test_resolve_memory_flag_beats_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    envmem = tmp_path / "envmem"
    flagmem = tmp_path / "flagmem"
    monkeypatch.setenv("LOVIA_MEMORY_DIR", str(envmem))
    mem = cli.resolve_memory(str(flagmem), no_memory=False)
    assert isinstance(mem, Memory)
    # The flag root is used; the env root is left untouched.
    assert flagmem.is_dir()
    assert not envmem.exists()


def test_resolve_memory_path_is_file(tmp_path: Path) -> None:
    f = tmp_path / "notadir"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(UserError, match="not a directory"):
        cli.resolve_memory(str(f), no_memory=False)


# ------------------------------------------------------------ built-in tools -


def test_resolve_tools_includes_builtins() -> None:
    names = {t.name for t in cli.resolve_tools()}
    assert {"now", "http_fetch"} <= names


def test_resolve_tools_includes_search_when_available() -> None:
    pytest.importorskip("ddgs")
    assert "web_search" in {t.name for t in cli.resolve_tools()}


def test_resolve_tools_skips_search_when_backend_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing() -> object:
        raise UserError("ddgs not installed")

    # A real key may have leaked from .env via the live-test helpers.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(cli, "duckduckgo_search", _missing)
    # The missing optional backend must degrade gracefully, not crash.
    names = {t.name for t in cli.resolve_tools()}
    assert names == {"now", "http_fetch"}


def test_resolve_tools_prefers_tavily_when_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> object:
        raise AssertionError("duckduckgo_search must not be constructed")

    monkeypatch.setenv("TAVILY_API_KEY", "k")
    monkeypatch.setattr(cli, "duckduckgo_search", _boom)
    names = {t.name for t in cli.resolve_tools()}
    assert names == {"now", "http_fetch", "web_search"}


# ---------------------------------------------------------- instructions -


def test_resolve_instructions_inline() -> None:
    assert cli.resolve_instructions("be terse", None) == "be terse"


def test_resolve_instructions_file(tmp_path: Path) -> None:
    f = tmp_path / "prompt.md"
    f.write_text("from file", encoding="utf-8")
    assert cli.resolve_instructions(None, str(f)) == "from file"


def test_resolve_instructions_file_missing(tmp_path: Path) -> None:
    with pytest.raises(UserError, match="instructions file not found"):
        cli.resolve_instructions(None, str(tmp_path / "nope.md"))


def test_resolve_instructions_convention_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LOVIA_INSTRUCTIONS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("conventional prompt", encoding="utf-8")
    assert cli.resolve_instructions(None, None) == "conventional prompt"


def test_resolve_instructions_generic_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LOVIA_INSTRUCTIONS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.resolve_instructions(None, None) == cli.GENERIC_INSTRUCTIONS


# ------------------------------------------------------------- workspace -


def test_resolve_workspace_disabled() -> None:
    assert cli.resolve_workspace(".", "trusted", no_workspace=True) is None


def test_resolve_workspace_default_mode_is_coding(tmp_path: Path) -> None:
    ws = cli.resolve_workspace(str(tmp_path), None, no_workspace=False)
    assert ws is not None
    # Matches the core ``Workspace.local`` default: writes allowed inside the
    # root, but shell and out-of-root reads go through approval — no unprompted
    # shell (that posture is ``trusted``, now opt-in).
    assert ws.policy.shell_default == "ask"
    assert ws.policy.read_outside == "ask"
    assert ws.policy.write == "allow"


def test_resolve_workspace_mode_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOVIA_WORKSPACE_MODE", "readonly")
    ws = cli.resolve_workspace(str(tmp_path), None, no_workspace=False)
    assert ws is not None
    assert ws.policy.write == "deny"
    assert ws.policy.allow_shell is False


def test_resolve_workspace_invalid_mode(tmp_path: Path) -> None:
    with pytest.raises(UserError, match="invalid workspace mode"):
        cli.resolve_workspace(str(tmp_path), "bogus", no_workspace=False)


def test_resolve_workspace_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(UserError, match="workspace directory not found"):
        cli.resolve_workspace(str(tmp_path / "nope"), "trusted", no_workspace=False)


def test_workspace_flags_map_to_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = cli.build_parser().parse_args(["--readonly"])
    assert cli._mode_flag(args) == "readonly"
    # The flag wins over the env var, like every other option.
    monkeypatch.setenv("LOVIA_WORKSPACE_MODE", "trusted")
    ws = cli.resolve_workspace(str(tmp_path), cli._mode_flag(args), args.no_workspace)
    assert ws is not None
    assert ws.policy.write == "deny"
    assert ws.policy.allow_shell is False

    assert cli._mode_flag(cli.build_parser().parse_args(["--trusted"])) == "trusted"
    assert cli._mode_flag(cli.build_parser().parse_args([])) is None


# -------------------------------------------------------------- --app -


def _write_module(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")


def test_load_app_requires_colon() -> None:
    with pytest.raises(UserError, match="MODULE:ATTRIBUTE"):
        cli.load_app_target("noColonHere")


def test_load_app_returns_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(
        tmp_path,
        "agentmod_a",
        "from lovia import Agent\nagent = Agent(name='custom', model='m')\n",
    )
    obj = cli.load_app_target("agentmod_a:agent")
    assert isinstance(obj, Agent)
    assert obj.name == "custom"


def test_load_app_calls_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(
        tmp_path,
        "agentmod_b",
        "from lovia import Agent\n"
        "def make():\n    return Agent(name='made', model='m')\n",
    )
    obj = cli.load_app_target("agentmod_b:make")
    assert isinstance(obj, Agent)
    assert obj.name == "made"


def test_load_app_bad_module() -> None:
    with pytest.raises(UserError, match="could not import module"):
        cli.load_app_target("definitely_not_a_module_xyz:agent")


def test_load_app_bad_attr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(tmp_path, "agentmod_c", "x = 1\n")
    with pytest.raises(UserError, match="has no attribute"):
        cli.load_app_target("agentmod_c:agent")


def test_load_app_wrong_type(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(tmp_path, "agentmod_d", "agent = 123\n")
    with pytest.raises(UserError, match="not an Agent"):
        cli.load_app_target("agentmod_d:agent")


# ------------------------------------------------------------- env files -


def test_load_env_file_sets_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("dotenv")
    env = tmp_path / "custom.env"
    env.write_text("LOVIA_TEST_VAR=hello\n", encoding="utf-8")
    monkeypatch.delenv("LOVIA_TEST_VAR", raising=False)
    sources = cli.load_env_files([str(env)])
    assert os.getenv("LOVIA_TEST_VAR") == "hello"
    # The summary names the actual file, not a generic ".env".
    assert sources["LOVIA_TEST_VAR"] == "custom.env"


def test_load_env_file_existing_env_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("dotenv")
    env = tmp_path / ".env"
    env.write_text("LOVIA_TEST_VAR2=fromfile\n", encoding="utf-8")
    monkeypatch.setenv("LOVIA_TEST_VAR2", "fromenv")
    monkeypatch.chdir(tmp_path)
    cli.load_env_files(None)
    assert os.getenv("LOVIA_TEST_VAR2") == "fromenv"


def test_load_env_file_missing_errors() -> None:
    pytest.importorskip("dotenv")
    with pytest.raises(UserError, match="env file not found"):
        cli.load_env_files(["/no/such/file.env"])


# --------------------------------------------------------------- parser -


def test_parser_repeatable_skills_dir() -> None:
    args = cli.build_parser().parse_args(["--skills-dir", "a", "--skills-dir", "b"])
    assert args.skills_dir == ["a", "b"]


def test_parser_workspace_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--readonly", "--trusted"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--trusted", "--no-workspace"])


def test_parser_defaults_are_none() -> None:
    args = cli.build_parser().parse_args([])
    assert args.host is None and args.port is None and args.model is None
    assert args.no_workspace is False
    assert args.readonly is False and args.trusted is False
    assert args.memory_dir is None and args.no_memory is False


def test_parser_memory_flags() -> None:
    args = cli.build_parser().parse_args(["--memory-dir", "mem", "--no-memory"])
    assert args.memory_dir == "mem"
    assert args.no_memory is True


# --------------------------------------------------- build_default_agent -


def _provider(model: str = "test-model") -> object:
    from lovia.providers import provider_from_string

    return provider_from_string(model)


def test_build_default_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOVIA_MEMORY_DIR", raising=False)
    (tmp_path / "skills").mkdir()
    args = cli.build_parser().parse_args([])
    provider = _provider()
    agent = cli.build_default_agent(args, ChatStore.in_memory(), provider)
    assert agent.name == "lovia"
    assert agent.model is provider
    assert agent.instructions == cli.GENERIC_INSTRUCTIONS
    # ./skills -> Skills, plus the on-by-default Todo + Scheduling + Memory plugins.
    assert {type(p).__name__ for p in agent.plugins} == {
        "Skills",
        "Todo",
        "Scheduling",
        "Memory",
    }
    # Always-on built-in tools (web_search only when its backend is installed).
    assert {"now", "http_fetch"} <= {t.name for t in agent.tools}
    assert agent.workspace is not None


def test_build_default_agent_no_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    args = cli.build_parser().parse_args(["--no-memory"])
    agent = cli.build_default_agent(args, ChatStore.in_memory(), _provider())
    assert all(not isinstance(p, Memory) for p in agent.plugins)


def test_build_default_agent_injects_current_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The default agent should know today's date — rendered into its system
    # prompt — so it searches the current year without a `now` round-trip.
    import asyncio
    from datetime import datetime

    from lovia.run_context import RunContext

    monkeypatch.chdir(tmp_path)
    args = cli.build_parser().parse_args([])
    agent = cli.build_default_agent(args, ChatStore.in_memory(), _provider())

    ctx = RunContext(context=None, entries=[], agent=agent)
    system_prompt = asyncio.run(agent.render_system_prompt(ctx))
    assert datetime.now().astimezone().strftime("%Y-%m-%d") in system_prompt


# ----------------------------------------------------------------- main -


def test_main_serves_custom_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(
        tmp_path,
        "agentmod_main",
        "from lovia import Agent\nagent = Agent(name='served', model='m')\n",
    )
    captured: dict[str, object] = {}

    def fake_serve(agent_or_agents: object, **kwargs: object) -> None:
        captured["agent"] = agent_or_agents
        captured.update(kwargs)

    monkeypatch.setattr(cli, "serve", fake_serve)
    rc = cli.main(
        ["--app", "agentmod_main:agent", "--host", "0.0.0.0", "--port", "9123"]
    )
    assert rc == 0
    assert isinstance(captured["agent"], Agent)
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9123


def test_main_reports_missing_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("LOVIA_MODEL", "OPENAI_DEFAULT_MODEL", "ANTHROPIC_DEFAULT_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err and "no model configured" in err


def test_main_port_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(
        tmp_path,
        "agentmod_port",
        "from lovia import Agent\nagent = Agent(name='p', model='m')\n",
    )
    monkeypatch.setenv("LOVIA_PORT", "7777")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "serve", lambda a, **k: captured.update(k))
    rc = cli.main(["--app", "agentmod_port:agent"])
    assert rc == 0
    assert captured["port"] == 7777


def test_main_token_flag_env_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(
        tmp_path,
        "agentmod_token",
        "from lovia import Agent\nagent = Agent(name='t', model='m')\n",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "serve", lambda a, **k: captured.update(k))

    # Default: no token — serve() decides (loopback = open, else generated).
    assert cli.main(["--app", "agentmod_token:agent"]) == 0
    assert captured["token"] is None

    monkeypatch.setenv("LOVIA_WEB_TOKEN", "from-env")
    assert cli.main(["--app", "agentmod_token:agent"]) == 0
    assert captured["token"] == "from-env"

    # The flag beats the env.
    assert cli.main(["--app", "agentmod_token:agent", "--token", "from-flag"]) == 0
    assert captured["token"] == "from-flag"


def test_main_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0


def test_main_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "lovia" in capsys.readouterr().out


def test_main_invalid_log_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    # The level is validated before anything else, so no model/app is needed.
    rc = cli.main(["--log-level", "bogus"])
    assert rc == 2
    assert "invalid log level" in capsys.readouterr().err


def test_main_passes_db_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(
        tmp_path,
        "agentmod_db",
        "from lovia import Agent\nagent = Agent(name='d', model='m')\n",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "serve", lambda a, **k: captured.update(k))
    rc = cli.main(["--app", "agentmod_db:agent", "--db", "chats.sqlite"])
    assert rc == 0
    assert captured["db_path"] == "chats.sqlite"


# ----------------------------------------------------- exposure warning -
# (is_loopback itself moved to lovia.web.auth; covered in test_auth.py.)


def test_warn_when_trusted_workspace_exposed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = cli.resolve_workspace(str(tmp_path), "trusted", no_workspace=False)
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.log, "warning", lambda *a, **k: calls.append(a))
    cli._warn_if_exposed("0.0.0.0", ws)
    assert calls  # warned


def test_no_warn_on_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = cli.resolve_workspace(str(tmp_path), "trusted", no_workspace=False)
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.log, "warning", lambda *a, **k: calls.append(a))
    cli._warn_if_exposed("127.0.0.1", ws)
    assert not calls


def test_no_warn_on_readonly_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = cli.resolve_workspace(str(tmp_path), "readonly", no_workspace=False)
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.log, "warning", lambda *a, **k: calls.append(a))
    cli._warn_if_exposed("0.0.0.0", ws)
    assert not calls


def test_no_warn_without_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.log, "warning", lambda *a, **k: calls.append(a))
    cli._warn_if_exposed("0.0.0.0", None)
    assert not calls


# ------------------------------------------ reliability / model knobs -


def test_resolve_max_retries_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOVIA_MAX_RETRIES", raising=False)
    # No flag, no env -> None: the agent's own retry posture applies.
    assert cli.resolve_max_retries(None) is None
    monkeypatch.setenv("LOVIA_MAX_RETRIES", "5")
    assert cli.resolve_max_retries(None) == 5  # env
    assert cli.resolve_max_retries(0) == 0  # flag wins; 0 disables retries


def test_resolve_max_retries_rejects_negative() -> None:
    with pytest.raises(UserError, match="must be >= 0"):
        cli.resolve_max_retries(-1)


def test_resolve_max_turns_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOVIA_MAX_TURNS", raising=False)
    assert cli.resolve_max_turns(None) == 50  # default
    monkeypatch.setenv("LOVIA_MAX_TURNS", "10")
    assert cli.resolve_max_turns(None) == 10  # env
    assert cli.resolve_max_turns(5) == 5  # flag wins


def test_resolve_max_turns_rejects_zero() -> None:
    with pytest.raises(UserError, match="must be >= 1"):
        cli.resolve_max_turns(0)


def test_resolve_max_tokens_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOVIA_MAX_TOKENS", raising=False)
    assert cli.resolve_max_tokens(None) is None  # provider default
    monkeypatch.setenv("LOVIA_MAX_TOKENS", "1024")
    assert cli.resolve_max_tokens(None) == 1024  # env
    assert cli.resolve_max_tokens(4096) == 4096  # flag wins


def test_resolve_max_tokens_rejects_non_positive() -> None:
    with pytest.raises(UserError, match="must be > 0"):
        cli.resolve_max_tokens(0)


def test_parser_reliability_flags() -> None:
    args = cli.build_parser().parse_args(
        [
            "--max-retries",
            "4",
            "--provider-timeout",
            "90",
            "--max-tokens",
            "2048",
            "--context-window",
            "128000",
            "--max-turns",
            "20",
            "--trust-env",
        ]
    )
    assert args.max_retries == 4
    assert args.provider_timeout == 90.0
    assert args.max_tokens == 2048
    assert args.context_window == 128_000
    assert args.max_turns == 20
    assert args.trust_env is True


def test_help_defaults_render_from_core_constants() -> None:
    # The numbers in --help are f-string-rendered from the core defaults, so
    # they can never drift from what the library actually does.
    help_text = cli.build_parser().format_help()
    assert f"default {cli.DEFAULT_TIMEOUT:g}" in help_text
    assert f"{cli.DEFAULT_RETRIES} retries" in help_text
    assert f"default {cli.DEFAULT_MAX_TURNS}" in help_text
    assert cli.DEFAULT_WORKSPACE_MODE in help_text


def test_help_is_grouped_and_plain_text() -> None:
    """The help is the CLI's front door: grouped, and free of docstring markup."""
    help_text = cli.build_parser("lovia web").format_help()
    assert help_text.startswith("usage: lovia web [options]")
    for group in ("model:", "agent:", "server:", "advanced:"):
        assert f"\n{group}\n" in help_text
    assert "``" not in help_text and "::" not in help_text
    # The examples and the precedence chain live in the epilog.
    assert "examples:" in help_text
    assert "flag > environment > .lovia/config.env > ./.env" in help_text


def test_parser_error_points_at_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser("lovia web").parse_args(["--nope"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "usage: lovia web [options]" in err
    assert "try 'lovia web --help'" in err


def test_build_default_agent_max_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    args = cli.build_parser().parse_args(["--max-tokens", "1234"])
    agent = cli.build_default_agent(args, ChatStore.in_memory(), _provider())
    assert agent.settings.max_tokens == 1234


def test_main_passes_retry_and_context_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOVIA_MODEL", "openai:gpt-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "serve", lambda a, **k: captured.update(k))
    rc = cli.main(
        ["--max-retries", "1", "--context-window", "111111", "--max-turns", "7"]
    )
    assert rc == 0
    retry = captured["retry"]
    assert isinstance(retry, RetryPolicy)
    assert retry.max_attempts == 2  # first attempt + 1 retry
    policy = captured["context_policy"]
    assert isinstance(policy, Compaction)
    assert policy.context_window == 111_111
    assert captured["max_turns"] == 7


def test_main_provider_timeout_and_trust_env_set_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOVIA_MODEL", "openai:gpt-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # setenv (not delenv) so monkeypatch restores/removes them on teardown even
    # though main() mutates os.environ directly.
    monkeypatch.setenv("LOVIA_PROVIDER_TIMEOUT", "60")
    monkeypatch.setenv("LOVIA_PROVIDER_TRUST_ENV", "")
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main(["--provider-timeout", "150", "--trust-env"])
    assert rc == 0
    assert os.environ["LOVIA_PROVIDER_TIMEOUT"] == "150.0"
    assert os.environ["LOVIA_PROVIDER_TRUST_ENV"] == "1"


def test_main_rejects_bad_provider_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOVIA_MODEL", "openai:gpt-x")
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main(["--provider-timeout", "0"])
    assert rc == 2
    assert "must be > 0" in capsys.readouterr().err


# ----------------------------------------- endpoint flags + onboarding -


class _FakeTty:
    def isatty(self) -> bool:
        return True

    def readline(self) -> str:  # pragma: no cover - never called in tests
        return "\n"


def test_parser_base_url_and_api_key() -> None:
    args = cli.build_parser().parse_args(
        ["--base-url", "https://api.deepseek.com", "--api-key", "sk-x"]
    )
    assert args.base_url == "https://api.deepseek.com"
    assert args.api_key == "sk-x"
    defaults = cli.build_parser().parse_args([])
    assert defaults.base_url is None and defaults.api_key is None


def test_parser_prog_override() -> None:
    assert cli.build_parser("lovia web").prog == "lovia web"
    assert cli.build_parser().prog == "python -m lovia.web"


def test_app_warns_about_endpoint_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warned: list[str] = []
    monkeypatch.setattr(
        cli.log, "warning", lambda msg, *a: warned.append(msg % a if a else msg)
    )
    args = cli.build_parser().parse_args(
        ["--app", "m:a", "--base-url", "http://x", "--api-key", "k"]
    )
    cli._warn_ignored_agent_flags(args)
    assert warned and "--base-url" in warned[0] and "--api-key" in warned[0]


def test_load_env_files_source_map_and_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "LOVIA_TEST_A=dotenv\nLOVIA_TEST_B=dotenv\n", encoding="utf-8"
    )
    monkeypatch.setenv("LOVIA_TEST_A", "process")
    monkeypatch.delenv("LOVIA_TEST_B", raising=False)
    try:
        sources = cli.load_env_files(None)
        # Process env beats ./.env.
        assert os.getenv("LOVIA_TEST_A") == "process"
        assert os.getenv("LOVIA_TEST_B") == "dotenv"
        assert "LOVIA_TEST_A" not in sources  # pre-existing -> plain env
        assert sources["LOVIA_TEST_B"] == ".env"
    finally:
        os.environ.pop("LOVIA_TEST_B", None)


def test_load_env_files_autoloads_lovia_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """.lovia/config.env is auto-loaded and wins over a legacy ./.env."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lovia").mkdir()
    (tmp_path / ".lovia" / "config.env").write_text(
        "LOVIA_TEST_C=canonical\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "LOVIA_TEST_C=legacy\nLOVIA_TEST_D=legacy\n", encoding="utf-8"
    )
    monkeypatch.delenv("LOVIA_TEST_C", raising=False)
    monkeypatch.delenv("LOVIA_TEST_D", raising=False)
    try:
        sources = cli.load_env_files(None)
        assert os.getenv("LOVIA_TEST_C") == "canonical"  # canonical file wins
        assert os.getenv("LOVIA_TEST_D") == "legacy"  # legacy ./.env still read
        assert sources["LOVIA_TEST_C"] == "config.env"
        assert sources["LOVIA_TEST_D"] == ".env"
    finally:
        os.environ.pop("LOVIA_TEST_C", None)
        os.environ.pop("LOVIA_TEST_D", None)


def test_main_reports_missing_api_key_when_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import io
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main(["--model", "openai:gpt-5.5"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no API key configured" in err
    # The concise hint names the non-interactive channels (flags + env vars).
    for channel in ("--api-key", "OPENAI_API_KEY"):
        assert channel in err


def test_main_configured_run_prints_summary_and_skips_wizard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOVIA_MODEL", "openai:gpt-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abcdefghijkl9876")

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("configured launches must not prompt or validate")

    monkeypatch.setattr(cli.setup, "interactive_setup", _boom)
    monkeypatch.setattr(cli.setup, "validate_connection", _boom)
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "openai:gpt-x (env)" in out
    assert "https://api.openai.com/v1 (default)" in out
    assert "sk-…9876 (env)" in out
    assert "serving on http://127.0.0.1:8000" in out


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        # 0.0.0.0 is a bind, not an address you can open — print one you can.
        ("0.0.0.0", "http://127.0.0.1:9000"),
        ("::", "http://127.0.0.1:9000"),
        # An IPv6 literal without brackets would swallow the port.
        ("::1", "http://[::1]:9000"),
        ("127.0.0.1", "http://127.0.0.1:9000"),
    ],
)
def test_main_summary_prints_a_browsable_url(
    host: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOVIA_MODEL", "openai:gpt-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abcdefghijkl9876")
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    assert cli.main(["--host", host, "--port", "9000"]) == 0
    assert f"serving on {expected}" in capsys.readouterr().out


def test_main_app_warns_when_workspace_exposed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(
        tmp_path,
        "agentmod_exposed",
        "from lovia import Agent\n"
        "from lovia.workspace import Workspace\n"
        "agent = Agent(name='x', model='m',"
        " workspace=Workspace.local('.', mode='trusted'))\n",
    )
    warned: list[object] = []
    monkeypatch.setattr(cli.log, "warning", lambda *a, **k: warned.append(a))
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main(["--app", "agentmod_exposed:agent", "--host", "0.0.0.0"])
    assert rc == 0
    assert any("non-loopback" in str(a[0]) for a in warned)


def test_main_app_prints_reduced_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(
        tmp_path,
        "agentmod_sum",
        "from lovia import Agent\nagent = Agent(name='s', model='m')\n",
    )
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main(["--app", "agentmod_sum:agent"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "agentmod_sum:agent" in out
    assert "serving on http://127.0.0.1:8000" in out
    assert "api key" not in out  # endpoint rows are the default agent's


def test_main_runs_wizard_when_config_missing_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", _FakeTty())
    completed = cli.setup.Connection(
        model="wizard-model",
        model_source="prompt",
        base_url="http://gw/v1",
        base_url_source="prompt",
    )
    calls: list[object] = []

    def fake_wizard(conn: object, **kwargs: object) -> object:
        calls.append(conn)
        return completed

    monkeypatch.setattr(cli.setup, "interactive_setup", fake_wizard)
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "serve", lambda a, **k: captured.update({"agent": a, **k}))
    rc = cli.main([])
    assert rc == 0
    assert len(calls) == 1
    agent = captured["agent"]
    assert isinstance(agent, Agent)
    assert getattr(agent.model, "base_url", None) == "http://gw/v1"


def test_main_wizard_interrupt_exits_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", _FakeTty())

    def interrupted(conn: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.setup, "interactive_setup", interrupted)
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    assert cli.main([]) == 130


def test_main_injects_endpoint_flags_into_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lovia.providers import OpenAIChatProvider

    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "serve", lambda a, **k: captured.update({"agent": a}))
    rc = cli.main(
        [
            "--model",
            "deepseek-v4-pro",
            "--base-url",
            "https://api.deepseek.com/",
            "--api-key",
            "sk-deep",
        ]
    )
    assert rc == 0
    agent = captured["agent"]
    assert isinstance(agent, Agent)
    provider = agent.model
    assert isinstance(provider, OpenAIChatProvider)
    assert provider.base_url == "https://api.deepseek.com"
    assert provider._api_key == "sk-deep"


def test_main_injects_anthropic_env_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lovia.providers import AnthropicProvider

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.example/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anth")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "serve", lambda a, **k: captured.update({"agent": a}))
    rc = cli.main(["--model", "anthropic:claude-x"])
    assert rc == 0
    agent = captured["agent"]
    assert isinstance(agent, Agent)
    provider = agent.model
    assert isinstance(provider, AnthropicProvider)
    assert provider.base_url == "https://gw.example/anthropic"
    assert provider._api_key == "sk-anth"


def test_main_reports_unknown_vendor_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main(["--model", "nosuchvendor:m", "--api-key", "sk-x"])
    assert rc == 2
    assert "Unknown model spec" in capsys.readouterr().err


def test_main_wizard_leaves_no_db_when_aborted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", _FakeTty())
    monkeypatch.setattr(
        cli.setup,
        "interactive_setup",
        lambda conn, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    cli.main([])
    assert not (tmp_path / ".lovia").exists()


def test_main_default_db_lands_under_dot_lovia(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOVIA_MODEL", "openai:gpt-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main([])
    assert rc == 0
    assert (tmp_path / ".lovia" / "lovia.db").is_file()
    assert ".lovia/lovia.db" in capsys.readouterr().out.replace("\\", "/")
