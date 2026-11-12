"""Tests for the ``python -m lovia.web`` command-line launcher."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

import pytest

pytest.importorskip("fastapi")

from lovia import Agent, Memory, RetryPolicy  # noqa: E402
from lovia.context import Compaction  # noqa: E402
from lovia.exceptions import UserError  # noqa: E402
from lovia.web import ChatStore  # noqa: E402
from lovia.web import __main__ as cli  # noqa: E402
from lovia.web import builder  # noqa: E402
from lovia.workspace import Workspace  # noqa: E402
from lovia.web.config import (  # noqa: E402
    AgentSettings,
    ModelProfile,
    RunSettings,
    SearchConfig,
    SkillsConfig,
    WebConfig,
    WorkspaceSettings,
    resolve_skills_dirs,
)


# ---------------------------------------------------------------- skills -


def test_resolve_skills_project_before_global(tmp_path: Path, fake_home: Path) -> None:
    project = tmp_path / ".agents" / "skills"
    project.mkdir(parents=True)
    user = fake_home / ".agents" / "skills"
    user.mkdir(parents=True)
    # The project's own first — on duplicate skill names it wins.
    assert resolve_skills_dirs(SkillsConfig(), tmp_path) == [project, user]
    # No project, no project skills.
    assert resolve_skills_dirs(SkillsConfig()) == [user]


def test_resolve_skills_reads_no_launch_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    assert resolve_skills_dirs(SkillsConfig()) == []
    assert resolve_skills_dirs(None) == []


def test_resolve_skills_removed_default_not_loaded(fake_home: Path) -> None:
    # Removing the global default from the list is how it gets disabled.
    (fake_home / ".agents" / "skills").mkdir(parents=True)
    assert resolve_skills_dirs(SkillsConfig(dirs=[])) == []


def test_skills_dirs_refuse_relative_entries() -> None:
    with pytest.raises(ValueError, match="not a global path"):
        SkillsConfig(dirs=["team-skills"])


def test_skills_dirs_drop_the_old_project_default() -> None:
    # Saved files carry the pre-0.9.179 default; it loads by itself now.
    cfg = SkillsConfig(dirs=[".agents/skills", "./.agents/skills", "~/.agents/skills"])
    assert cfg.dirs == ["~/.agents/skills"]


def test_resolve_skills_custom_dir_expands_user(fake_home: Path) -> None:
    target = fake_home / "team"
    target.mkdir()
    assert resolve_skills_dirs(SkillsConfig(dirs=["~/team"])) == [target]


def test_resolve_skills_missing_dir_skipped_never_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A stale path in config.json must never make the server unbootable.
    monkeypatch.chdir(tmp_path)
    with caplog.at_level("INFO", logger="lovia.web.config"):
        assert resolve_skills_dirs(SkillsConfig(dirs=["~/definitely-nope"])) == []
    assert "skipped" in caplog.text


def test_resolve_skills_dedupes_by_identity(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    dirs = [str(tmp_path / "x"), f"{tmp_path}{os.sep}.{os.sep}x"]
    assert resolve_skills_dirs(SkillsConfig(dirs=dirs)) == [tmp_path / "x"]
    # The project's own directory listed globally too: loaded once.
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    listed = SkillsConfig(dirs=[str(tmp_path / ".agents" / "skills")])
    assert resolve_skills_dirs(listed, tmp_path) == [tmp_path / ".agents" / "skills"]


# ----------------------------------------------------------------- memory -


def test_resolve_memory_disabled() -> None:
    assert builder.resolve_memory(False, "./anywhere") is None


def test_default_agent_memory_lives_in_the_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_home: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    agent = builder.build_default_agent(
        builder.Deployment(workspace=str(tmp_path), home=home),
        ChatStore.in_memory(),
        _provider(),
    )
    (mem,) = [p for p in agent.plugins if isinstance(p, Memory)]
    assert mem.index is not None  # default builds both the notes and archive tiers
    assert (home / "memory").is_dir()
    # Nothing under the launch directory, nor the default home.
    assert not (tmp_path / ".lovia").exists()
    assert not (fake_home / ".lovia" / "memory").exists()


def test_resolve_memory_explicit_dir(tmp_path: Path) -> None:
    target = tmp_path / "mem"
    mem = builder.resolve_memory(True, str(target))
    assert isinstance(mem, Memory)
    assert target.is_dir()


def test_resolve_memory_path_is_file(tmp_path: Path) -> None:
    f = tmp_path / "notadir"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(UserError, match="not a directory"):
        builder.resolve_memory(True, str(f))


# ------------------------------------------------------------ built-in tools -


def test_resolve_tools_includes_builtins() -> None:
    names = {t.name for t in builder.resolve_tools()}
    assert {"now", "web_fetch"} <= names


def test_resolve_tools_auto_uses_ddg_without_a_key() -> None:
    pytest.importorskip("ddgs")
    assert "web_search" in {t.name for t in builder.resolve_tools()}


def test_resolve_tools_off_disables_search() -> None:
    names = {t.name for t in builder.resolve_tools(SearchConfig(backend="off"))}
    assert names == {"now", "web_fetch"}


def test_resolve_tools_skips_search_when_backend_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing() -> object:
        raise UserError("ddgs not installed")

    monkeypatch.setattr(builder, "duckduckgo_search", _missing)
    # The missing optional backend must degrade gracefully, not crash.
    names = {t.name for t in builder.resolve_tools()}
    assert names == {"now", "web_fetch"}


def test_resolve_tools_prefers_tavily_when_key_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> object:
        raise AssertionError("duckduckgo_search must not be constructed")

    monkeypatch.setattr(builder, "duckduckgo_search", _boom)
    search = SearchConfig(tavily_api_key="k")  # backend "auto"
    names = {t.name for t in builder.resolve_tools(search)}
    assert names == {"now", "web_fetch", "web_search"}


def test_resolve_tools_tavily_without_key_warns_and_degrades(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="lovia.web.builder"):
        names = {t.name for t in builder.resolve_tools(SearchConfig(backend="tavily"))}
    assert "web_search" not in names
    assert "no API key" in caplog.text


# ---------------------------------------------------------- instructions -


def test_resolve_instructions_custom_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert builder.resolve_instructions("  be terse \n") == "be terse"


def test_resolve_instructions_convention_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("conventional prompt", encoding="utf-8")
    assert builder.resolve_instructions() == "conventional prompt"


def test_resolve_instructions_custom_then_agents_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("project rules", encoding="utf-8")
    assert builder.resolve_instructions("be terse") == "be terse\n\nproject rules"


def test_resolve_instructions_generic_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert builder.resolve_instructions() == builder.GENERIC_INSTRUCTIONS


# ------------------------------------------------------------- workspace -


def _ws_config(**fields: object) -> WebConfig:
    return WebConfig(workspace=WorkspaceSettings.model_validate(fields))


def test_resolve_workspace_disabled() -> None:
    assert builder.resolve_workspace(builder.Deployment(workspace=None)) is None


def test_resolve_workspace_default_mode_is_coding(tmp_path: Path) -> None:
    ws = builder.resolve_workspace(builder.Deployment(workspace=str(tmp_path)))
    assert ws is not None
    # Matches the core ``Workspace.local`` default: writes allowed inside the
    # root, but shell and out-of-root reads go through approval — no unprompted
    # shell (that posture is ``trusted``, now opt-in).
    assert ws.policy.shell_default == "ask"
    assert ws.policy.read_outside == "ask"
    assert ws.policy.write == "allow"


def test_resolve_workspace_mode_from_config(tmp_path: Path) -> None:
    ws = builder.resolve_workspace(
        builder.Deployment(workspace=str(tmp_path)),
        _ws_config(default_mode="readonly"),
    )
    assert ws is not None
    assert ws.policy.write == "deny"
    assert ws.policy.allow_shell is False


def test_coding_auto_allows_inspection_and_dev_tools_by_default(
    tmp_path: Path,
) -> None:
    ws = builder.resolve_workspace(builder.Deployment(workspace=str(tmp_path)))
    assert ws is not None
    for cmd in ("ls", "pwd", "echo hi", "git status", "pytest -q", "ruff check ."):
        assert ws.policy.decide_command(cmd).action == "allow", cmd
    # Everything else still asks, and the baseline still bites.
    assert ws.policy.decide_command("python app.py").action == "ask"
    assert ws.policy.decide_command("git push origin main").action == "ask"
    assert ws.policy.decide_command("git push --force").action == "deny"


def test_auto_allow_bundles_from_config(tmp_path: Path) -> None:
    deployment = builder.Deployment(workspace=str(tmp_path))
    none = builder.resolve_workspace(deployment, _ws_config(auto_allow=[]))
    assert none is not None and none.policy.decide_command("ls").action == "ask"
    only = builder.resolve_workspace(deployment, _ws_config(auto_allow=["read-only"]))
    assert only is not None
    assert only.policy.decide_command("ls").action == "allow"
    assert only.policy.decide_command("pytest").action == "ask"


@pytest.mark.parametrize("bad", ["bogus", "cat"])
def test_auto_allow_rejects_unknown_bundles(bad: str) -> None:
    with pytest.raises(ValueError, match="auto_allow"):
        _ws_config(auto_allow=["read-only", bad])


def test_resolve_workspace_invalid_mode() -> None:
    with pytest.raises(ValueError, match="default_mode"):
        _ws_config(default_mode="bogus")


def test_resolve_workspace_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(UserError, match="workspace directory not found"):
        builder.resolve_workspace(builder.Deployment(workspace=str(tmp_path / "no")))


def test_readonly_flag_beats_the_configured_mode(tmp_path: Path) -> None:
    deployment = builder.Deployment(workspace=str(tmp_path), readonly=True)
    ws = builder.resolve_workspace(deployment, _ws_config(default_mode="trusted"))
    assert ws is not None
    assert ws.policy.write == "deny"
    assert ws.policy.allow_shell is False


@pytest.mark.parametrize(
    ("deployment", "modes", "trusted_runs_as", "lock"),
    [
        (builder.Deployment(), ("readonly", "coding", "trusted"), "trusted", None),
        (builder.Deployment(readonly=True), ("readonly",), "readonly", "readonly"),
        (builder.Deployment(workspace=None), (), None, "no_workspace"),
    ],
)
def test_deployment_bounds_modes_and_edits(
    deployment: builder.Deployment,
    modes: tuple[str, ...],
    trusted_runs_as: str | None,
    lock: str | None,
) -> None:
    assert deployment.modes() == modes
    assert deployment.effective_mode("trusted") == trusted_runs_as
    assert deployment.workspace_lock() == lock


@pytest.mark.parametrize(
    ("deployment", "roots"),
    [
        (builder.Deployment(), "any"),
        (builder.Deployment(lock_workspace=True), None),
        (builder.Deployment(workspace=None), None),
    ],
)
def test_deployment_bounds_the_directories_a_chat_opens(
    deployment: builder.Deployment, roots: str | None
) -> None:
    assert deployment.roots() == roots
    assert deployment.remotes() == roots


@pytest.mark.parametrize(
    ("argv", "roots", "modes"),
    [
        ([], "any", ("readonly", "coding", "trusted")),
        (["--lock-workspace"], None, ("readonly", "coding", "trusted")),
        (["--readonly"], "any", ("readonly",)),
        # The bind address changes nothing: tightening is explicit.
        (["--host", "0.0.0.0"], "any", ("readonly", "coding", "trusted")),
        (
            ["--host", "0.0.0.0", "--lock-workspace"],
            None,
            ("readonly", "coding", "trusted"),
        ),
    ],
)
def test_main_wires_the_switch_by_the_flags_alone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_home: Path,
    write_config: Callable[..., Path],
    argv: list[str],
    roots: str | None,
    modes: tuple[str, ...],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config()
    seen = _capture_serving(monkeypatch)
    assert cli.main(argv) == 0
    switch = seen["workspaces"]
    assert switch.roots == roots  # type: ignore[attr-defined]
    assert switch.remotes == roots  # type: ignore[attr-defined]
    assert switch.modes == modes  # type: ignore[attr-defined]
    general = fake_home / ".lovia" / "workspace"
    expected = general.resolve() if roots else None
    assert switch.general == expected  # type: ignore[attr-defined]
    assert switch.agent_for is not None  # type: ignore[attr-defined]
    # Listed, not made: only a chat there creates it.
    assert not general.exists()


@pytest.mark.parametrize("where", ["home", "dir"])
def test_main_launched_from_home_works_in_the_general_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_home: Path,
    write_config: Callable[..., Path],
    where: str,
) -> None:
    monkeypatch.chdir(fake_home)
    write_config()
    seen = _capture_serving(monkeypatch)
    # DIR given — even the home directory itself — is taken as it is.
    assert cli.main([] if where == "home" else [str(fake_home)]) == 0
    agent = seen["agent"]
    root = Path(agent.workspace.root).resolve()  # type: ignore[attr-defined]
    general = fake_home / ".lovia" / "workspace"
    if where == "home":
        assert root == general.resolve() and general.is_dir()
    else:
        assert root == fake_home.resolve() and not general.exists()


def test_project_agent_reads_the_chat_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lovia import Skills

    launch, proj = tmp_path / "launch", tmp_path / "proj"
    launch.mkdir()
    (launch / "AGENTS.md").write_text("launch rules", encoding="utf-8")
    skill = proj / ".agents" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: a demo\n---\nbody\n", encoding="utf-8"
    )
    (proj / "AGENTS.md").write_text("proj rules", encoding="utf-8")
    monkeypatch.chdir(launch)
    config = _agent_config(memory=False, instructions="custom")
    agent = builder.build_default_agent(
        builder.Deployment(workspace=str(launch)),
        ChatStore.in_memory(),
        _provider(),
        config=config,
    )
    assert "launch rules" in str(agent.instructions)
    assert not any(isinstance(p, Skills) for p in agent.plugins)
    moved = builder.project_agent(agent, proj, config)
    assert moved.instructions == "custom\n\nproj rules"
    (skills,) = [p for p in moved.plugins if isinstance(p, Skills)]
    assert moved.plugins[0] is skills
    # Back in a directory with no skills: none.
    back = builder.project_agent(moved, launch, config)
    assert not any(isinstance(p, Skills) for p in back.plugins)
    assert back.instructions == "custom\n\nlaunch rules"


# -------------------------------------------------------------- --app -


def _write_module(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")


def _capture_serving(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub ``create_app`` + ``serve``: the dict collects the served agent(s)
    under ``"agent"`` plus both calls' keyword arguments."""
    captured: dict[str, object] = {}

    def fake_create_app(agent_or_agents: object, **kwargs: object) -> object:
        captured["agent"] = agent_or_agents
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli, "serve", lambda app, **kwargs: captured.update(kwargs))
    return captured


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


_BUILT_APP_MODULE = (
    "from lovia import Agent\n"
    "from lovia.testing import ScriptedProvider\n"
    "from lovia.web import create_app\n"
    "from lovia.web.store import ChatStore\n"
    "agent = Agent(name='built', model=ScriptedProvider([]))\n"
    "app = create_app(agent, store=ChatStore.in_memory(), title='Mine')\n"
    "def make():\n    return app\n"
)


def test_load_app_returns_a_built_app_uncalled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A FastAPI app is callable (ASGI); it must not be taken for a factory.
    monkeypatch.chdir(tmp_path)
    _write_module(tmp_path, "appmod_a", _BUILT_APP_MODULE)
    app = cli.load_app_target("appmod_a:app")
    assert app is sys.modules["appmod_a"].app
    assert cli.load_app_target("appmod_a:make") is app  # an app factory


def test_main_serves_a_built_app_as_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(tmp_path, "appmod_b", _BUILT_APP_MODULE)
    served: dict[str, object] = {}
    monkeypatch.setattr(cli, "create_app", _unexpected_create_app)
    monkeypatch.setattr(cli, "serve", lambda app, **k: served.update(app=app, **k))
    rc = cli.main(["--app", "appmod_b:app", "--title", "Other", "--port", "9124"])
    assert rc == 0
    assert served["app"] is sys.modules["appmod_b"].app
    assert served["port"] == 9124
    err = capsys.readouterr().err
    assert "ignoring options it was built without: --title" in err


def _unexpected_create_app(*a: object, **k: object) -> None:
    raise AssertionError("a built --app must not be rebuilt")


def test_main_serves_under_a_root_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(tmp_path, "appmod_c", _BUILT_APP_MODULE)
    served: dict[str, object] = {}
    monkeypatch.setattr(cli, "serve", lambda app, **k: served.update(k))
    # Normalized to one leading slash, none trailing.
    assert cli.main(["--app", "appmod_c:app", "--root-path", "lovia/"]) == 0
    assert served["root_path"] == "/lovia"
    assert "(proxied under /lovia)" in capsys.readouterr().out

    monkeypatch.setenv("LOVIA_ROOT_PATH", "/chat")
    assert cli.main(["--app", "appmod_c:app"]) == 0
    assert served["root_path"] == "/chat"


def test_main_refuses_a_built_app_without_auth_off_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    monkeypatch.chdir(tmp_path)
    _write_module(tmp_path, "appmod_c", _BUILT_APP_MODULE)
    monkeypatch.setattr(uvicorn, "run", _unexpected_create_app)
    rc = cli.main(["--app", "appmod_c:app", "--host", "0.0.0.0"])
    assert rc == 2
    assert "without authentication" in capsys.readouterr().err


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


# --------------------------------------------------------------- parser -


def test_parser_rejects_the_removed_skills_dir_flag() -> None:
    # Skill directories live in config.json (Settings → Skills) now.
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--skills-dir", "x"])


def test_parser_workspace_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--readonly", "--no-workspace"])


def test_parser_defaults_are_none() -> None:
    args = cli.build_parser().parse_args([])
    assert args.host is None and args.port is None
    assert args.dir is None and args.workspace is None
    assert args.no_workspace is False and args.readonly is False
    assert args.home is None


def test_parser_has_no_bind_dependent_workspace_flag() -> None:
    # The bind address changes nothing, so there is nothing to opt out of.
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--allow-workspace-switch"])


def test_parser_takes_the_workspace_as_dir() -> None:
    assert cli.build_parser().parse_args(["~/notes"]).dir == "~/notes"
    # The older spelling still parses (hidden from --help).
    assert cli.build_parser().parse_args(["--workspace", "x"]).workspace == "x"


def test_parser_has_no_model_connection_flags() -> None:
    """The connection lives in config.json — flags for it are gone (0.9.37)."""
    for flag in (
        "--model",
        "--base-url",
        "--api-key",
        "--context-window",
        "--env-file",
    ):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([flag, "x"])


def test_parser_prog_override() -> None:
    assert cli.build_parser("lovia web").prog == "lovia web"
    assert cli.build_parser().prog == "python -m lovia.web"


def test_help_defaults_render_from_core_constants() -> None:
    # The numbers in --help are f-string-rendered from the core defaults, so
    # they can never drift from what the library actually does.
    help_text = cli.build_parser().format_help()
    assert f"default {cli.DEFAULT_TIMEOUT:g}" in help_text


# ------------------------------------------------------- removed options -


@pytest.mark.parametrize(
    ("argv", "where"),
    [
        (["--max-turns", "20"], "run.max_turns"),
        (["--max-retries=3"], "run.max_retries"),
        (["--no-followups"], "run.followups"),
        (["--trusted"], "workspace.default_mode"),
        (["--auto-allow", "none"], "workspace.auto_allow"),
        (["--no-memory"], "agent.memory"),
        (["--memory-dir", "m"], "--home DIR"),
        (["--no-subagents"], "agent.subagents"),
        (["--instructions", "x"], "agent.instructions"),
        (["--instructions-file", "x"], "agent.instructions"),
        (["--max-tokens", "9"], "models[].max_tokens"),
    ],
)
def test_main_points_a_removed_option_at_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    where: str,
) -> None:
    monkeypatch.setattr(cli, "serve", _unexpected_create_app)
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert argv[0].split("=")[0] in err and where in err


def test_main_points_removed_env_vars_at_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "serve", _unexpected_create_app)
    monkeypatch.setenv("LOVIA_MAX_TURNS", "10")
    monkeypatch.setenv("LOVIA_WORKSPACE_MODE", "readonly")
    assert cli.main([]) == 2
    err = capsys.readouterr().err
    # Every hit at once, each with its new home.
    assert "LOVIA_MAX_TURNS → Settings → Agent (config.json run.max_turns)" in err
    assert "LOVIA_WORKSPACE_MODE → --readonly" in err


def test_removed_options_after_double_dash_are_not_flags() -> None:
    cli.check_removed_options(["--", "--max-turns"], env={})


def test_main_rejects_two_workspace_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "serve", _unexpected_create_app)
    assert cli.main(["a", "--workspace", "b"]) == 2
    assert "two workspace roots" in capsys.readouterr().err


def test_help_is_grouped_and_plain_text() -> None:
    """The help is the CLI's front door: grouped, and free of docstring markup."""
    help_text = cli.build_parser("lovia web").format_help()
    assert help_text.startswith("usage: lovia web [options]")
    for group in ("model:", "agent:", "server:", "advanced:"):
        assert f"\n{group}\n" in help_text
    assert "``" not in help_text and "::" not in help_text
    # The examples and the configuration story live in the epilog.
    assert "examples:" in help_text
    assert "config.json" in help_text


def test_parser_error_points_at_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser("lovia web").parse_args(["--nope"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "usage: lovia web [options]" in err
    assert "try 'lovia web --help'" in err


# --------------------------------------------------- build_default_agent -


def _provider(model: str = "test-model") -> object:
    from lovia.providers import provider_from_string

    return provider_from_string(model)


_DEPLOY = builder.Deployment()


def _agent_config(**agent: object) -> WebConfig:
    return WebConfig(agent=AgentSettings.model_validate(agent))


def test_build_default_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    provider = _provider()
    agent = builder.build_default_agent(_DEPLOY, ChatStore.in_memory(), provider)
    assert agent.name == "lovia"
    assert agent.model is provider
    assert agent.instructions == builder.GENERIC_INSTRUCTIONS
    # ./.agents/skills -> Skills, plus the on-by-default Todo + Scheduling +
    # Memory + Subagents plugins.
    assert {type(p).__name__ for p in agent.plugins} == {
        "Skills",
        "Todo",
        "Scheduling",
        "Memory",
        "Subagents",
    }
    # Always-on built-in tools (web_search only when its backend is installed).
    assert {"now", "web_fetch"} <= {t.name for t in agent.tools}
    assert agent.workspace is not None


def test_build_default_agent_no_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    agent = builder.build_default_agent(
        _DEPLOY, ChatStore.in_memory(), _provider(), config=_agent_config(memory=False)
    )
    assert all(not isinstance(p, Memory) for p in agent.plugins)


def test_build_default_agent_subagents_child_composition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lovia import Subagents

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    agent = builder.build_default_agent(_DEPLOY, ChatStore.in_memory(), _provider())
    (plugin,) = [p for p in agent.plugins if isinstance(p, Subagents)]
    (child,) = plugin._catalog.values()
    # The child keeps the basic capabilities (Skills, Todo) and sheds the
    # ones that must not ride along (Subagents, Scheduling, Memory).
    assert {type(p).__name__ for p in child.plugins} == {"Skills", "Todo"}
    assert child.workspace is agent.workspace
    parent_only = {"show_chart", "show_table", "show_html"}  # no one watches a child
    assert child.tools == [t for t in agent.tools if t.name not in parent_only]


def test_build_default_agent_no_subagents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lovia import Subagents

    monkeypatch.chdir(tmp_path)
    agent = builder.build_default_agent(
        _DEPLOY,
        ChatStore.in_memory(),
        _provider(),
        config=_agent_config(subagents=False),
    )
    assert all(not isinstance(p, Subagents) for p in agent.plugins)


def test_build_default_agent_shows_ui_components_parent_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lovia import Subagents

    monkeypatch.chdir(tmp_path)
    agent = builder.build_default_agent(
        _DEPLOY, ChatStore.in_memory(), _provider(), config=_agent_config(memory=False)
    )
    ui_tools = {"show_chart", "show_table", "show_html"}
    assert ui_tools <= {t.name for t in agent.tools}
    (plugin,) = [p for p in agent.plugins if isinstance(p, Subagents)]
    (child,) = plugin._catalog.values()
    assert not ui_tools & {t.name for t in child.tools}


def test_build_default_agent_wires_the_vision_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    config = WebConfig.model_validate(
        {
            "agent": {"memory": False},
            "models": [
                {"id": "chat", "model": "text-model"},
                {"id": "eyes", "model": "glm-4.6v", "api_key": "sk-v"},
            ],
            "roles": {"chat": "chat", "vision": "eyes"},
        }
    )
    from lovia.providers import provider_from_string

    # The main model must be text-only, or images would go inline instead.
    blind = provider_from_string("text-model", supports_vision=False)
    agent = builder.build_default_agent(
        _DEPLOY, ChatStore.in_memory(), blind, config=config
    )
    assert "describe_image" in {t.name for t in agent.tools}


def test_build_default_agent_injects_current_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The default agent should know today's date — rendered into its system
    # prompt — so it searches the current year without a `now` round-trip.
    import asyncio
    from datetime import datetime

    from lovia.run_context import RunContext

    monkeypatch.chdir(tmp_path)
    agent = builder.build_default_agent(_DEPLOY, ChatStore.in_memory(), _provider())

    ctx = RunContext(deps=None, entries=[], agent=agent)
    system_prompt = asyncio.run(agent.render_system_prompt(ctx))
    assert datetime.now().astimezone().strftime("%Y-%m-%d") in system_prompt


def test_build_default_agent_declares_render_surface(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The default agent only ever runs in the bundled UI — its system prompt
    # says so, or the model assumes plain text and refuses to embed images.
    import asyncio

    from lovia.run_context import RunContext
    from lovia.web import SURFACE_NOTE

    monkeypatch.chdir(tmp_path)
    agent = builder.build_default_agent(_DEPLOY, ChatStore.in_memory(), _provider())

    ctx = RunContext(deps=None, entries=[], agent=agent)
    system_prompt = asyncio.run(agent.render_system_prompt(ctx))
    assert SURFACE_NOTE in system_prompt


def test_build_default_agent_max_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    config = WebConfig.model_validate(
        {"models": [{"id": "m", "model": "test-model", "max_tokens": 1234}]}
    )
    agent = builder.build_default_agent(
        _DEPLOY, ChatStore.in_memory(), _provider(), config=config
    )
    assert agent.settings.max_tokens == 1234
    (child,) = next(p for p in agent.plugins if type(p).__name__ == "Subagents").agents
    assert child.settings.max_tokens == 1234


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
    captured = _capture_serving(monkeypatch)
    rc = cli.main(
        ["--app", "agentmod_main:agent", "--host", "0.0.0.0", "--port", "9123"]
    )
    assert rc == 0
    assert isinstance(captured["agent"], Agent)
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9123
    # Off loopback with no --token: the CLI generated one for the app.
    assert isinstance(captured["token"], str) and captured["token"]


def test_main_missing_model_serves_the_setup_ui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing configured: serve the setup UI — even on a TTY (single entry)."""
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", _FakeTty())  # a TTY changes nothing
    captured = _capture_serving(monkeypatch)
    rc = cli.main([])
    assert rc == 0
    assert captured["agent"] == {}  # nothing to chat with yet
    runtime = captured["config_runtime"]
    assert runtime is not None and not runtime.configured  # type: ignore[union-attr]
    out = capsys.readouterr().out
    assert "not configured" in out
    assert "serving on http://127.0.0.1:8000" in out


def test_main_port_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _write_module(
        tmp_path,
        "agentmod_port",
        "from lovia import Agent\nagent = Agent(name='p', model='m')\n",
    )
    monkeypatch.setenv("LOVIA_PORT", "7777")
    captured = _capture_serving(monkeypatch)
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
    captured = _capture_serving(monkeypatch)

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
    captured = _capture_serving(monkeypatch)
    rc = cli.main(["--app", "agentmod_db:agent", "--db", "chats.sqlite"])
    assert rc == 0
    assert captured["db_path"] == "chats.sqlite"


# ----------------------------------------------------- exposure warning -
# (is_loopback itself moved to lovia.web.auth; covered in test_auth.py.)


def test_warn_when_trusted_workspace_exposed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = Workspace.local(str(tmp_path), mode="trusted")
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.log, "warning", lambda *a, **k: calls.append(a))
    cli._warn_if_exposed("0.0.0.0", ws)
    assert calls  # warned


def test_no_warn_on_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = Workspace.local(str(tmp_path), mode="trusted")
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.log, "warning", lambda *a, **k: calls.append(a))
    cli._warn_if_exposed("127.0.0.1", ws)
    assert not calls


def test_no_warn_on_readonly_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = Workspace.local(str(tmp_path), mode="readonly")
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.log, "warning", lambda *a, **k: calls.append(a))
    cli._warn_if_exposed("0.0.0.0", ws)
    assert not calls


@pytest.mark.parametrize(
    ("host", "deployment", "warns"),
    [
        ("0.0.0.0", builder.Deployment(), True),
        ("0.0.0.0", builder.Deployment(readonly=True), False),
        ("0.0.0.0", builder.Deployment(workspace=None), False),
        ("127.0.0.1", builder.Deployment(), False),
    ],
)
def test_default_agent_warns_by_the_widest_allowed_mode(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    deployment: builder.Deployment,
    warns: bool,
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.log, "warning", lambda *a, **k: calls.append(a))
    cli._warn_if_remote_capable(host, deployment)
    assert bool(calls) is warns


def test_no_warn_without_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.log, "warning", lambda *a, **k: calls.append(a))
    cli._warn_if_exposed("0.0.0.0", None)
    assert not calls


# ------------------------------------------ reliability / model knobs -


def test_resolve_retry() -> None:
    # None: the agent's own retry posture applies.
    assert builder.resolve_retry(RunSettings()) is None
    policy = builder.resolve_retry(RunSettings(max_retries=0))
    assert policy is not None and policy.max_attempts == 1  # 0 disables retries


def test_run_settings_reject_out_of_range() -> None:
    with pytest.raises(ValueError):
        RunSettings(max_turns=0)
    with pytest.raises(ValueError):
        RunSettings(max_retries=-1)


def test_parser_reliability_flags() -> None:
    args = cli.build_parser().parse_args(["--provider-timeout", "90", "--trust-env"])
    assert args.provider_timeout == 90.0
    assert args.trust_env is True


# ------------------------------------------------- config-driven launch -


class _FakeTty:
    def isatty(self) -> bool:
        return True

    def readline(self) -> str:  # pragma: no cover - never called in tests
        return "\n"


def test_app_warns_about_agent_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warned: list[str] = []
    monkeypatch.setattr(
        cli.log, "warning", lambda msg, *a: warned.append(msg % a if a else msg)
    )
    args = cli.build_parser().parse_args(["--app", "m:a", "--readonly", "."])
    cli._warn_ignored_agent_flags(args)
    assert warned and "DIR" in warned[0] and "--readonly" in warned[0]


def test_main_missing_api_key_serves_the_setup_ui(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_config: Callable[..., Path],
) -> None:
    """A partial config (key missing on an official host) also defers to the UI."""
    import io
    import sys

    monkeypatch.chdir(tmp_path)
    write_config(model="openai:gpt-5.5", api_key=None)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    captured = _capture_serving(monkeypatch)
    rc = cli.main([])
    assert rc == 0
    assert captured["agent"] == {}


def test_main_configured_run_passes_the_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config()
    captured = _capture_serving(monkeypatch)
    assert cli.main([]) == 0
    runtime = captured["config_runtime"]
    assert runtime is not None and runtime.configured  # type: ignore[union-attr]


def test_main_check_rejects_custom_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    # Guarded before the app target is even imported: the bogus module is fine.
    rc = cli.main(["--app", "nosuchmod:agent", "--check"])
    assert rc == 2
    assert "--check" in capsys.readouterr().err


def test_main_check_reports_and_exits_without_serving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    write_config: Callable[..., Path],
) -> None:
    from lovia.web.config import ValidationOutcome

    monkeypatch.chdir(tmp_path)
    write_config()
    monkeypatch.setattr(
        "lovia.web.config.check.validate_connection",
        lambda conn, transport=None: (ValidationOutcome.OK, "HTTP 200"),
    )

    def _no_serve(*a: object, **k: object) -> None:
        raise AssertionError("--check must exit before serving")

    monkeypatch.setattr(cli, "serve", _no_serve)
    rc = cli.main(["--check"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "configuration check" in out
    assert "openai:gpt-x" in out
    assert "✓ endpoint reachable" in out
    # The home's one config file, with its presence.
    assert "~/.lovia/config.json (present)" in out
    assert "./.lovia" not in out


def test_main_check_missing_config_is_exit_2_without_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    def _no_serve(*a: object, **k: object) -> None:
        raise AssertionError("--check must exit before serving")

    monkeypatch.setattr(cli, "serve", _no_serve)
    rc = cli.main(["--check"])
    assert rc == 2
    assert "missing: model" in capsys.readouterr().out


def test_main_raises_the_open_file_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    raised: list[int] = []
    monkeypatch.setattr(cli, "raise_soft_limit", lambda: raised.append(1))
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    cli.main(["--check"])
    capsys.readouterr()
    assert raised == [1]


def test_main_configured_run_prints_summary_and_skips_wizard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    write_config: Callable[..., Path],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config()

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("configured launches must not prompt or validate")

    monkeypatch.setattr("lovia.web.config.check.validate_connection", _boom)
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "openai:gpt-x" in out
    assert "https://api.openai.com/v1" in out
    assert "sk-…9876" in out
    assert "~/.lovia/config.json" in out
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
    write_config: Callable[..., Path],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config()
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


def test_main_injects_profile_endpoint_into_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    from lovia.providers import OpenAIChatProvider

    monkeypatch.chdir(tmp_path)
    write_config(
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/",
        api_key="sk-deep",
    )
    captured = _capture_serving(monkeypatch)
    rc = cli.main([])
    assert rc == 0
    agent = captured["agent"]
    assert isinstance(agent, Agent)
    provider = agent.model
    assert isinstance(provider, OpenAIChatProvider)
    assert provider.base_url == "https://api.deepseek.com"
    assert provider._api_key == "sk-deep"


def test_main_injects_anthropic_profile_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    from lovia.providers import AnthropicProvider

    monkeypatch.chdir(tmp_path)
    write_config(
        model="anthropic:claude-x",
        base_url="https://gw.example/anthropic",
        api_key="sk-anth",
    )
    captured = _capture_serving(monkeypatch)
    rc = cli.main([])
    assert rc == 0
    agent = captured["agent"]
    assert isinstance(agent, Agent)
    provider = agent.model
    assert isinstance(provider, AnthropicProvider)
    assert provider.base_url == "https://gw.example/anthropic"
    assert provider._api_key == "sk-anth"


def test_main_reports_unknown_vendor_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    write_config: Callable[..., Path],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config(model="nosuchvendor:m", api_key="sk-x")
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main([])
    assert rc == 2
    assert "Unknown model spec" in capsys.readouterr().err


def test_main_passes_retry_and_context_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config(context_window=111_111, run={"max_retries": 1, "max_turns": 7})
    captured = _capture_serving(monkeypatch)
    rc = cli.main([])
    assert rc == 0
    retry = captured["retry"]
    assert isinstance(retry, RetryPolicy)
    assert retry.max_attempts == 2  # first attempt + 1 retry
    policy = captured["context_policy"]
    assert isinstance(policy, Compaction)
    assert policy.context_window == 111_111
    assert captured["max_turns"] == 7


def test_main_provider_timeout_and_trust_env_set_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config()
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    write_config: Callable[..., Path],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config()
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main(["--provider-timeout", "0"])
    assert rc == 2
    assert "must be > 0" in capsys.readouterr().err


def test_main_default_db_lands_in_the_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_home: Path,
    capsys: pytest.CaptureFixture[str],
    write_config: Callable[..., Path],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config()
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    rc = cli.main([])
    assert rc == 0
    db = fake_home / ".lovia" / "lovia.db"
    assert db.is_file()
    assert not (tmp_path / ".lovia" / "lovia.db").exists()
    assert str(db) in capsys.readouterr().out


def test_main_ignores_a_launch_dir_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_home: Path,
    write_config: Callable[..., Path],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config()
    ChatStore.sqlite(tmp_path / ".lovia" / "lovia.db")  # from before 0.9.148
    monkeypatch.setattr(cli, "serve", lambda *a, **k: None)
    assert cli.main([]) == 0
    assert (fake_home / ".lovia" / "lovia.db").is_file()


def test_main_home_holds_config_history_memory_and_general_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_home: Path,
    capsys: pytest.CaptureFixture[str],
    write_config: Callable[..., Path],
) -> None:
    home = tmp_path / "home"
    write_config("openai:in-home", home=home)
    write_config("openai:in-default-home")  # never read with --home
    monkeypatch.chdir(tmp_path)
    seen = _capture_serving(monkeypatch)
    assert cli.main(["--home", str(home)]) == 0
    agent = seen["agent"]
    assert getattr(agent.model, "model", None) == "in-home"
    assert (home / "lovia.db").is_file()
    assert (home / ".gitignore").is_file()
    (mem,) = [p for p in agent.plugins if isinstance(p, Memory)]
    assert Path(mem.root).resolve() == (home / "memory").resolve()  # type: ignore[attr-defined]
    switch = seen["workspaces"]
    assert switch.general == (home / "workspace").resolve()  # type: ignore[attr-defined]
    assert not (fake_home / ".lovia" / "lovia.db").exists()
    assert str(home / "config.json") in capsys.readouterr().out


def test_main_home_must_be_a_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    monkeypatch.setattr(cli, "serve", _unexpected_create_app)
    assert cli.main(["--home", "file"]) == 2
    assert "--home is not a directory" in capsys.readouterr().err


def test_main_names_an_unread_launch_dir_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_config: Callable[..., Path],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config()
    write_config("openai:old-project", home=tmp_path / ".lovia")
    warned: list[str] = []
    monkeypatch.setattr(cli.log, "warning", lambda msg, *a, **k: warned.append(msg % a))
    seen = _capture_serving(monkeypatch)
    assert cli.main([]) == 0
    assert getattr(seen["agent"].model, "model", None) == "gpt-x"
    assert any("--home ./.lovia" in w for w in warned)
    # Asked for it by name: no line.
    warned.clear()
    assert cli.main(["--home", ".lovia"]) == 0
    assert getattr(seen["agent"].model, "model", None) == "old-project"
    assert not any("--home ./.lovia" in w for w in warned)


def test_main_launched_in_the_home_directory_names_no_legacy_config(
    monkeypatch: pytest.MonkeyPatch,
    fake_home: Path,
    write_config: Callable[..., Path],
) -> None:
    # From ~, ./.lovia *is* the default home.
    monkeypatch.chdir(fake_home)
    write_config()
    warned: list[str] = []
    monkeypatch.setattr(cli.log, "warning", lambda msg, *a, **k: warned.append(msg % a))
    _capture_serving(monkeypatch)
    assert cli.main([]) == 0
    assert not any("--home ./.lovia" in w for w in warned)


def test_main_wires_the_aux_role_into_titles_and_followups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config(
        models=[
            {"id": "chat", "model": "openai:gpt-x", "api_key": "sk-x"},
            {"id": "small", "model": "openai:gpt-small", "api_key": "sk-small"},
        ],
        roles={"chat": "chat", "aux": "small"},
    )
    seen = _capture_serving(monkeypatch)
    assert cli.main([]) == 0
    aux = seen["followup_model"]
    assert aux is not None
    assert getattr(aux, "model", None) == "gpt-small"
    # One aux model serves both jobs, as the Settings hint promises.
    assert seen["title_model"] is aux


def test_main_boots_with_the_saved_extra_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    """Boot builds through the same path as hot-reload, so a saved profile
    field reaches the served provider on restart, not only after Settings."""
    monkeypatch.chdir(tmp_path)
    write_config(
        models=[
            {
                "id": "chat",
                "model": "openai:gpt-x",
                "api_key": "sk-x",
                "extra_body": {"reasoning_effort": "medium"},
            }
        ]
    )
    seen = _capture_serving(monkeypatch)
    assert cli.main([]) == 0
    assert seen["agent"].model._extra_body == {"reasoning_effort": "medium"}


# ------------------------------------------------------------- followups -


def test_resolve_aux_model_unassigned() -> None:
    assert builder.resolve_aux_model(None) is None


def test_resolve_aux_model_builds_a_provider_with_the_profile_extras() -> None:
    profile = ModelProfile(
        id="s",
        model="openai:gpt-small",
        api_key="sk-small",
        extra_body={"reasoning_effort": "low"},
    )
    provider = builder.resolve_aux_model(profile)
    assert provider is not None
    assert provider.model == "gpt-small"
    assert provider._extra_body == {"reasoning_effort": "low"}


def test_resolve_aux_model_bad_spec_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unusable profile must not take the whole server down with it."""
    profile = ModelProfile(id="s", model="no-such-vendor:whatever")
    with caplog.at_level("WARNING", logger="lovia.web.builder"):
        assert builder.resolve_aux_model(profile) is None
    assert "falling back" in caplog.text


def test_main_passes_followups_to_serve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config()
    seen = _capture_serving(monkeypatch)
    assert cli.main([]) == 0
    assert seen["followups"] is True
    assert seen["followup_model"] is None

    seen.clear()
    write_config(run={"followups": False})
    assert cli.main([]) == 0
    assert seen["followups"] is False


# ------------------------------------------------- per-chat modes (CLI) -


def test_main_lets_chats_pick_a_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    monkeypatch.chdir(tmp_path)
    write_config(workspace={"default_mode": "coding", "auto_allow": []})
    captured = _capture_serving(monkeypatch)
    assert cli.main([]) == 0
    switch = captured["workspaces"]
    assert switch.modes == ("readonly", "coding", "trusted")  # type: ignore[attr-defined]
    # Built from the live configuration: coding with its auto-allow bundles.
    ws = switch.build(tmp_path, "coding")  # type: ignore[attr-defined]
    assert ws.policy.decide_command("ls").action == "ask"
    assert switch.build(tmp_path, "readonly").policy.write == "deny"  # type: ignore[attr-defined]

    captured.clear()
    assert cli.main(["--no-workspace"]) == 0
    assert captured["workspaces"] is None
