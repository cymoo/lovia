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


# ----------------------------------------------------------------- model -


def test_resolve_model_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_MODEL", "env-model")
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "openai-model")
    # CLI flag wins over everything.
    assert cli.resolve_model("cli-model") == "cli-model"
    # Then LOVIA_MODEL.
    assert cli.resolve_model(None) == "env-model"
    monkeypatch.delenv("LOVIA_MODEL")
    # Then OPENAI_DEFAULT_MODEL.
    assert cli.resolve_model(None) == "openai-model"


def test_resolve_model_falls_back_to_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOVIA_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_DEFAULT_MODEL", "claude-x")
    # A bare Anthropic id gets its vendor prefix so it routes to the right
    # adapter instead of warn-routing to the OpenAI-compatible one.
    assert cli.resolve_model(None) == "anthropic:claude-x"


def test_resolve_model_errors_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LOVIA_MODEL", "OPENAI_DEFAULT_MODEL", "ANTHROPIC_DEFAULT_MODEL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(UserError, match="no model configured"):
        cli.resolve_model(None)


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

    monkeypatch.setattr(cli, "duckduckgo_search", _missing)
    # The missing optional backend must degrade gracefully, not crash.
    names = {t.name for t in cli.resolve_tools()}
    assert names == {"now", "http_fetch"}


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


def test_resolve_workspace_default_mode_is_trusted(tmp_path: Path) -> None:
    ws = cli.resolve_workspace(str(tmp_path), None, no_workspace=False)
    assert ws is not None
    # 'trusted' is the only mode whose shell defaults to allow.
    assert ws.policy.shell_default == "allow"


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
    cli.load_env_files([str(env)])
    assert os.getenv("LOVIA_TEST_VAR") == "hello"


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


def test_parser_rejects_bad_workspace_mode() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--workspace-mode", "bogus"])


def test_parser_defaults_are_none() -> None:
    args = cli.build_parser().parse_args([])
    assert args.host is None and args.port is None and args.model is None
    assert args.no_workspace is False
    assert args.memory_dir is None and args.no_memory is False


def test_parser_memory_flags() -> None:
    args = cli.build_parser().parse_args(["--memory-dir", "mem", "--no-memory"])
    assert args.memory_dir == "mem"
    assert args.no_memory is True


# --------------------------------------------------- build_default_agent -


def test_build_default_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOVIA_MODEL", "test-model")
    monkeypatch.delenv("LOVIA_MEMORY_DIR", raising=False)
    (tmp_path / "skills").mkdir()
    args = cli.build_parser().parse_args([])
    agent = cli.build_default_agent(args, ChatStore.in_memory())
    assert agent.name == "lovia"
    assert agent.model == "test-model"
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
    monkeypatch.setenv("LOVIA_MODEL", "test-model")
    args = cli.build_parser().parse_args(["--no-memory"])
    agent = cli.build_default_agent(args, ChatStore.in_memory())
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
    monkeypatch.setenv("LOVIA_MODEL", "test-model")
    args = cli.build_parser().parse_args([])
    agent = cli.build_default_agent(args, ChatStore.in_memory())

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


def test_is_loopback() -> None:
    assert cli._is_loopback("127.0.0.1")
    assert cli._is_loopback("localhost")
    assert cli._is_loopback("::1")
    assert not cli._is_loopback("0.0.0.0")
    assert not cli._is_loopback("::")
    assert not cli._is_loopback("192.168.1.5")


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
    assert cli.resolve_max_retries(None) == 2  # default
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


def test_resolve_context_window_explicit_wins() -> None:
    assert cli.resolve_context_window(123_456) == 123_456


def test_resolve_context_window_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_CONTEXT_WINDOW", "100000")
    assert cli.resolve_context_window(None) == 100_000


def test_resolve_context_window_defaults_to_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOVIA_CONTEXT_WINDOW", raising=False)
    # No flag, no env -> None: Compaction asks the provider at call time and
    # falls back to reactive overflow handling when the window is unknown.
    assert cli.resolve_context_window(None) is None


def test_resolve_context_window_rejects_zero() -> None:
    with pytest.raises(UserError, match="must be >= 1"):
        cli.resolve_context_window(0)


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


def test_build_default_agent_max_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOVIA_MODEL", "test-model")
    args = cli.build_parser().parse_args(["--max-tokens", "1234"])
    agent = cli.build_default_agent(args, ChatStore.in_memory())
    assert agent.settings.max_tokens == 1234


def test_main_passes_retry_and_context_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOVIA_MODEL", "openai:gpt-x")
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
