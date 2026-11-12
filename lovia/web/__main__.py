"""``lovia web`` (or ``python -m lovia.web``) — launch the lovia chat UI.

Builds a ready-made agent (model, skills, long-term memory, a todo checklist,
current-date awareness, model-driven scheduled runs, built-in tools — time,
HTTP fetch, web search — and a workspace) and serves it with the bundled web
UI. ``--app module:attribute`` serves your own ``Agent`` — or a whole app
from :func:`~lovia.web.create_app`, as built — instead.

The model connection is not a flag: it lives in ``config.json`` (see
:mod:`lovia.web.config`) and is managed in one place — the web UI's
Settings. An unconfigured launch serves the setup screen; ``--check`` is
the read-only diagnosis. The user-facing contract is the ``--help`` text:
:data:`DESCRIPTION`, :data:`EPILOG` and the option groups in
:func:`build_parser`.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import Any, NoReturn, cast

from .. import __version__
from .._fdlimit import raise_soft_limit
from ..agent import Agent
from ..exceptions import UserError
from ..http_config import DEFAULT_TIMEOUT
from ..log_config import enable_logging
from ..providers import Provider
from ..tools import HumanChannel
from ..workspace import WorkspaceMode
from . import config as webconfig
from .app import (
    _display_host,
    _generated_token,
    create_app,
    serve,
)
from fastapi import FastAPI  # after .app: its import names a missing [web] extra

from .api import RouterDeps
from .auth import is_loopback
from .builder import Deployment
from .followups import FollowupFn
from .store import ChatStore
from .workspaces import WorkspaceSwitch

log = logging.getLogger("lovia.web.cli")

LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

DESCRIPTION = """\
Launch the lovia chat UI in your browser.

Serves ready-made agents — skills, long-term memory, todos, web search,
scheduled runs, and a workspace rooted at DIR (default: the current
directory). Everything about them — models, the agents themselves and each
one's prompt, permissions and run limits — is set in the browser (Settings)
and saved to config.json in lovia's home, applying from the next run
without a restart.
"""

EPILOG = """\
examples:
  lovia web                                # first run: configure in the browser
  lovia web ~/notes --port 9000
  lovia web ~/notes --readonly             # let it read, never write
  lovia web --home ~/lovia-test            # a separate config, history, memory
  lovia web --app myagents:assistant       # serve your own agent
  lovia web --app myserver:app             # ... or your own create_app() app
  lovia web init-ui ui                     # scaffold a UI directory for ChatUI(assets=)

configuration:
  Models, web search, global skill directories and the agents (each with its
  prompt, model, permission mode, auto-allowed commands, switches and run
  limits) live in config.json in lovia's home (~/.lovia, or --home DIR), beside the
  chat history and memory. Managed in the web UI's Settings; inspect with
  --check. A project's own AGENTS.md and .agents/skills load in every chat
  working there. The flags above are the server's own, plus the workspace
  locks a deployment needs — the same on every --host.
"""

# Options that moved into config.json (Settings) in 0.9.146. Each still
# answers with where it went instead of argparse's bare "unrecognized
# arguments" — kept through the 0.9 series, dropped at 0.10.
_SETTINGS = "Settings → {pane} (config.json {key})"
REMOVED_OPTIONS: dict[str, str] = {
    "--trusted": _SETTINGS.format(pane="Agents", key="agents[].mode"),
    "--auto-allow": _SETTINGS.format(pane="Agents", key="agents[].auto_allow"),
    "--max-turns": _SETTINGS.format(pane="Agents", key="agents[].max_turns"),
    "--max-retries": _SETTINGS.format(pane="Agents", key="agents[].max_retries"),
    "--no-followups": _SETTINGS.format(pane="Agents", key="agents[].followups"),
    "--memory-dir": "--home DIR (memory lives in <home>/memory)",
    "--no-memory": _SETTINGS.format(pane="Agents", key="agents[].memory"),
    "--no-subagents": _SETTINGS.format(pane="Agents", key="agents[].subagents"),
    "--instructions": _SETTINGS.format(pane="Agents", key="agents[].instructions"),
    "--instructions-file": _SETTINGS.format(pane="Agents", key="agents[].instructions"),
    "--max-tokens": _SETTINGS.format(pane="Connections", key="models[].max_tokens"),
}
REMOVED_ENV: dict[str, str] = {
    "LOVIA_WORKSPACE_MODE": "--readonly to lock read-only; otherwise "
    + REMOVED_OPTIONS["--trusted"],
    "LOVIA_AUTO_ALLOW": REMOVED_OPTIONS["--auto-allow"],
    "LOVIA_MAX_TURNS": REMOVED_OPTIONS["--max-turns"],
    "LOVIA_MAX_RETRIES": REMOVED_OPTIONS["--max-retries"],
    "LOVIA_FOLLOWUPS": REMOVED_OPTIONS["--no-followups"],
    "LOVIA_MEMORY_DIR": REMOVED_OPTIONS["--memory-dir"],
    "LOVIA_INSTRUCTIONS_FILE": REMOVED_OPTIONS["--instructions-file"],
    "LOVIA_MAX_TOKENS": REMOVED_OPTIONS["--max-tokens"],
}


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Verbatim description/epilog, aligned option column, capped line width."""

    def __init__(self, prog: str) -> None:
        width = min(shutil.get_terminal_size().columns - 2, 96)
        super().__init__(prog, max_help_position=28, width=max(width, 50))


class _Parser(argparse.ArgumentParser):
    """Argparse that points at ``--help`` — the usage line lists no options."""

    def error(self, message: str) -> NoReturn:
        self.exit(
            2,
            f"{self.format_usage()}{self.prog}: error: {message}\n"
            f"try '{self.prog} --help' for the option list\n",
        )


class CliError(UserError):
    """A user-facing CLI misconfiguration; rendered without a traceback."""


def _first(*values: str | None) -> str | None:
    """Return the first non-empty value (the precedence helper)."""
    for value in values:
        if value:
            return value
    return None


def check_removed_options(
    argv: list[str], env: Mapping[str, str] | None = None
) -> None:
    """Refuse options and variables that moved into Settings, saying where.

    Silently ignoring them would leave a deployment running on settings it
    no longer has; argparse's own "unrecognized arguments" would not say
    what replaced them. Every hit is reported at once.
    """
    env = os.environ if env is None else env
    hits: list[str] = []
    for arg in argv:
        if arg == "--":
            break
        name = arg.split("=", 1)[0]
        if name in REMOVED_OPTIONS:
            hits.append(f"{name} → {REMOVED_OPTIONS[name]}")
    for name, where in REMOVED_ENV.items():
        if name in env:
            hits.append(f"{name} → {where}")
    if hits:
        raise CliError(
            "these options are no longer read:\n  " + "\n  ".join(hits),
            hint="remove them; a custom --app sets its run limits in code "
            "(create_app(max_turns=..., retry=..., followups=...))",
        )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise CliError(f"invalid integer for {name}: {raw!r}") from exc


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    prog = prog or "python -m lovia.web"
    p = _Parser(
        prog=prog,
        usage=f"{prog} [options]",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=_HelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"lovia {__version__}")

    model = p.add_argument_group(
        "model", "the connection lives in config.json, managed in the web UI"
    )
    model.add_argument(
        "--check",
        action="store_true",
        help="print the configured connection, probe the endpoint, and exit "
        "(0 reachable, 2 not)",
    )

    p.add_argument(
        "dir",
        nargs="?",
        metavar="DIR",
        help="the directory new chats work in; the browser can open others "
        "(env LOVIA_WORKSPACE, default: the current directory — or "
        "~/.lovia/workspace when that is your home or the root directory)",
    )
    agent = p.add_argument_group(
        "agent", "the default agent; --app serves your own instead, ignoring the rest"
    )
    agent.add_argument(
        "--app",
        metavar="MODULE:ATTR",
        help="serve your own Agent, mapping, or create_app() app (or a factory "
        "of one) instead (env LOVIA_APP)",
    )
    agent.add_argument(
        "--workspace", metavar="DIR", help=argparse.SUPPRESS
    )  # the positional DIR's older spelling
    ws = agent.add_mutually_exclusive_group()
    ws.add_argument(
        "--readonly",
        action="store_true",
        help="lock every run read-only — no writes, no shell; Settings can't change it",
    )
    ws.add_argument(
        "--no-workspace", action="store_true", help="no filesystem/shell workspace"
    )
    agent.add_argument(
        "--lock-workspace",
        action="store_true",
        help="keep every chat in the launch directory: the browser can't open "
        "another, here or on another machine",
    )
    agent.add_argument(
        "--home",
        metavar="DIR",
        help="lovia's home: config.json, the chat history, memory and the "
        "general workspace (default ~/.lovia); one per setup kept apart",
    )

    server = p.add_argument_group("server")
    server.add_argument(
        "--host", help="bind address (env LOVIA_HOST, default 127.0.0.1)"
    )
    server.add_argument(
        "--port", type=int, help="port to listen on (env LOVIA_PORT, default 8000)"
    )
    server.add_argument(
        "--root-path",
        metavar="PATH",
        help="path prefix a reverse proxy serves the app under (and strips), "
        "e.g. /lovia (env LOVIA_ROOT_PATH)",
    )
    server.add_argument(
        "--token",
        metavar="TOKEN",
        help="API auth token; not needed on loopback, generated and printed "
        "otherwise (env LOVIA_WEB_TOKEN — preferred: flags are visible in the "
        "process list)",
    )
    server.add_argument("--title", help="page title (env LOVIA_TITLE, default lovia)")
    server.add_argument(
        "--db",
        metavar="FILE",
        help="SQLite file for chat history (env LOVIA_DB, default <home>/lovia.db)",
    )

    advanced = p.add_argument_group("advanced")
    advanced.add_argument(
        "--provider-timeout",
        type=float,
        metavar="SECS",
        help="per-request model provider timeout "
        f"(env LOVIA_PROVIDER_TIMEOUT, default {DEFAULT_TIMEOUT:g})",
    )
    advanced.add_argument(
        "--trust-env",
        action="store_true",
        help="honor HTTP(S)_PROXY / NO_PROXY for provider calls "
        "(env LOVIA_PROVIDER_TRUST_ENV)",
    )
    advanced.add_argument(
        "--log-level",
        metavar="LEVEL",
        help="debug, info, warning, error (env LOVIA_LOG_LEVEL, default info)",
    )
    return p


def load_app_target(
    target: str,
) -> Agent[Any] | Mapping[str, Agent[Any]] | FastAPI:
    """Import ``module:attribute`` and return the Agent, mapping, or app it names.

    An app (from :func:`~lovia.web.create_app`, or any FastAPI app) is served
    as built. Any other callable is a factory, called with no arguments.
    """
    if ":" not in target:
        raise CliError(
            f"--app must be MODULE:ATTRIBUTE, got {target!r}",
            hint="e.g. --app myagents:assistant",
        )
    module_name, _, attr = target.partition(":")
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise CliError(f"could not import module {module_name!r}: {exc}") from exc
    try:
        obj = getattr(module, attr)
    except AttributeError as exc:
        raise CliError(f"module {module_name!r} has no attribute {attr!r}") from exc
    # An app is itself callable (ASGI) — it must not be mistaken for a factory.
    if callable(obj) and not isinstance(obj, (Agent, Mapping, FastAPI)):
        obj = obj()
    if not isinstance(obj, (Agent, Mapping, FastAPI)):
        raise CliError(
            f"--app target {target!r} is not an Agent, a mapping of agents, or "
            f"an app (got {type(obj).__name__})"
        )
    return cast("Agent[Any] | Mapping[str, Agent[Any]] | FastAPI", obj)


def _workspace_arg(args: argparse.Namespace) -> str | None:
    """The workspace root from ``DIR`` (or ``--workspace``), else the env."""
    if args.dir and args.workspace and args.dir != args.workspace:
        raise CliError(
            f"two workspace roots given: {args.dir!r} and --workspace "
            f"{args.workspace!r}",
            hint="pass DIR alone",
        )
    return _first(args.dir, args.workspace, os.getenv("LOVIA_WORKSPACE"))


def _warn_ignored_agent_flags(args: argparse.Namespace) -> None:
    flags = [
        ("DIR", args.dir is not None or args.workspace is not None),
        ("--readonly", args.readonly),
        ("--no-workspace", args.no_workspace),
        ("--lock-workspace", args.lock_workspace),
        ("--home", args.home is not None),
    ]
    ignored = [name for name, given in flags if given]
    if ignored:
        log.warning("--app set; ignoring default-agent options: %s", ", ".join(ignored))


def _launch_dir(args: argparse.Namespace, general: Path) -> str:
    """The effective launch directory: ``DIR`` as given (the home directory
    too — that is the person's explicit choice), else the current one —
    unless that is the home or root directory, where the ``general``
    workspace stands in (created now: it is in use) so files never land
    loose there."""
    given = _workspace_arg(args)
    if given:
        return given
    cwd = Path.cwd().resolve()
    if cwd in (Path.home().resolve(), Path(cwd.anchor)):
        general.mkdir(parents=True, exist_ok=True)
        return str(general)
    return "."


def _home(args: argparse.Namespace) -> Path:
    """lovia's home, from ``--home`` (default ``~/.lovia``), expanded.

    Without ``--home``, a ``./.lovia/config.json`` here is what older
    versions read first; it is not read now, and silently ignoring it would
    leave the server on another configuration, so say how to keep it.
    """
    if args.home:
        # Absolute, so the banner, the db path and memory name one place
        # whatever the working directory (and no "run/../home" in them).
        home = Path(os.path.abspath(Path(args.home).expanduser()))
        if home.exists() and not home.is_dir():
            raise CliError(f"--home is not a directory: {home}")
        return home
    home = webconfig.DEFAULT_HOME.expanduser()
    legacy = Path(".lovia")
    if (legacy / "config.json").is_file() and (legacy.resolve() != home.resolve()):
        log.warning(
            "./.lovia/config.json is not read: lovia keeps its configuration, "
            "chat history and memory in %s — start with --home ./.lovia to "
            "use that directory instead",
            webconfig.display_path(home),
        )
    return home


def _warn_ignored_app_flags(args: argparse.Namespace) -> None:
    """An ``--app`` that is already an app was configured by create_app()."""
    flags = [
        ("--token", args.token is not None),
        ("--title", args.title is not None),
        ("--db", args.db is not None),
    ]
    ignored = [name for name, given in flags if given]
    if ignored:
        log.warning(
            "--app is a built app; ignoring options it was built without: %s "
            "(pass them to create_app instead)",
            ", ".join(ignored),
        )


def _warn_if_exposed(host: str, workspace: object) -> None:
    """Warn when a write/shell-capable workspace is reachable off-host.

    Non-loopback binds are always token-guarded (one is generated when none
    is given), but a leaked or shared token then grants file edits and
    shell — worth a heads-up whenever such a workspace leaves loopback.
    """
    policy = getattr(workspace, "policy", None)
    if policy is None or is_loopback(host):
        return
    write_capable = (
        getattr(policy, "write", "deny") != "deny"
        or getattr(policy, "write_outside", "deny") != "deny"
    )
    if getattr(policy, "allow_shell", False) or write_capable:
        log.warning(
            "binding to non-loopback host %r with a write/shell-capable workspace: "
            "anyone holding the API token can make the agent edit files or run "
            "shell commands. Use --readonly, --no-workspace, or bind to 127.0.0.1.",
            host,
        )


def _warn_if_remote_capable(host: str, deployment: Deployment) -> None:
    """The default agent's version of :func:`_warn_if_exposed`: judged by the
    widest mode the deployment allows, since Settings may switch to it."""
    if is_loopback(host) or "coding" not in deployment.modes():
        return
    log.warning(
        "binding to non-loopback host %r with a write/shell-capable workspace: "
        "anyone holding the API token can make the agent edit files or run "
        "shell commands, here or on hosts this machine can reach. Narrow it "
        "with --readonly, --lock-workspace or --no-workspace.",
        host,
    )


def _agents_desc(config: webconfig.WebConfig) -> str:
    """Human line for the startup summary: the default agent first."""
    first, *rest = [a.display_name for a in config.agents]
    return ", ".join([f"{first} (default)" if rest else first, *rest])


def _workspace_desc(deployment: Deployment, config: webconfig.WebConfig) -> str:
    """Human line for the startup summary, e.g. ``/path/to/dir (coding)``:
    how the default agent works there."""
    if deployment.workspace is None:
        return "(none)"
    agent = config.default_agent
    mode = deployment.effective_mode(agent.mode) or "none"
    desc = mode
    if mode == "coding":
        desc = f"coding, auto-allow: {', '.join(agent.auto_allow) or 'none'}"
    if mode != agent.mode:
        desc += f"; {agent.mode} in config.json, capped by --readonly"
    elif deployment.readonly:
        desc += ", locked by --readonly"
    if deployment.lock_workspace:
        desc += "; every chat here (--lock-workspace)"
    root = Path(deployment.workspace).resolve()
    if root == deployment.general_workspace().resolve():
        desc += "; the general workspace — pass DIR to work in a project"
    return f"{root} ({desc})"


def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["init-ui"]:
        from .scaffold import main as init_ui

        return init_ui(argv[1:], prog=f"{prog or 'python -m lovia.web'} init-ui")
    try:
        # Before parsing: argparse would reject a moved option without
        # saying where it went.
        check_removed_options(argv)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args = build_parser(prog).parse_args(argv)
    try:
        level = (_first(args.log_level, os.getenv("LOVIA_LOG_LEVEL")) or "info").upper()
        if level not in LOG_LEVELS:
            raise CliError(
                f"invalid log level: {level!r}",
                hint=f"choose one of: {', '.join(lv.lower() for lv in LOG_LEVELS)}",
            )
        # %(run_source)s tags every line emitted inside a run's task context
        # with what started it and who is running — [user/lovia],
        # [subagent:<session>/<agent>], [followups] — so parallel background
        # work stays legible.
        enable_logging(
            level,
            format="%(asctime)s %(levelname)-7s %(name)s: %(run_source)s%(message)s",
        )
        from .supervisor import run_source_log_filter

        for handler in logging.getLogger("lovia").handlers:
            handler.addFilter(run_source_log_filter())

        # Before anything opens a socket: agents running in parallel outrun a
        # default soft limit (256 on macOS) in a single burst of tool calls.
        raise_soft_limit()

        if args.provider_timeout is not None:
            if args.provider_timeout <= 0:
                raise CliError(
                    f"--provider-timeout must be > 0, got {args.provider_timeout}"
                )
            # The providers read these when constructing their HTTP client.
            os.environ["LOVIA_PROVIDER_TIMEOUT"] = str(args.provider_timeout)
        if args.trust_env:
            os.environ["LOVIA_PROVIDER_TRUST_ENV"] = "1"

        host = _first(args.host, os.getenv("LOVIA_HOST")) or "127.0.0.1"
        port = args.port if args.port is not None else _env_int("LOVIA_PORT", 8000)
        title = _first(args.title, os.getenv("LOVIA_TITLE")) or "lovia"
        db_path = _first(args.db, os.getenv("LOVIA_DB"))
        # None on a non-loopback bind → one is generated (and printed) below.
        token = _first(args.token, os.getenv("LOVIA_WEB_TOKEN"))
        root = (_first(args.root_path, os.getenv("LOVIA_ROOT_PATH")) or "").strip("/")
        root_path = f"/{root}" if root else ""
        # A wildcard bind is not a browsable address: show one that is.
        url = f"http://{_display_host(host)}:{port}"
        if root_path:
            url += f" (proxied under {root_path})"

        agent_or_agents: Agent[Any] | Mapping[str, Agent[Any]] = {}
        # An --app that is already an app: served as built, no create_app here.
        built_app: FastAPI | None = None
        # For the default agent we build the store up front (rather than letting
        # create_app build it) so the schedule_run tool can close over the same
        # ChatStore the scheduler polls. Custom --app agents keep using db_path.
        store: ChatStore | None = None
        # ask_human bridge for the configured agents. A custom --app brings
        # its own agents; wiring their channel is the library API's job
        # (create_app's question_channel=), so it stays None here.
        question_channel: HumanChannel | None = None
        # The aux-role model (titles + follow-ups); None means the agent's own.
        aux_model: Provider | None = None
        # The runtime-reconfiguration surface behind /api/config — built for
        # the configured agents only (a custom --app configures itself in code).
        config_runtime: webconfig.ConfigRuntime | None = None
        # Run limits: each configured agent's come from config.json (and
        # follow Settings live); a custom --app agent gets the CLI defaults —
        # anything else is create_app()'s job in its own code.
        max_turns: int | Mapping[str, int] = webconfig.DEFAULT_MAX_TURNS
        followups: bool | FollowupFn = True
        # Per-chat permission modes, when the agents have a workspace; a
        # custom --app opts in through create_app(workspaces=).
        workspaces: WorkspaceSwitch | None = None
        app_target = _first(args.app, os.getenv("LOVIA_APP"))
        if app_target:
            if args.check:
                raise CliError(
                    "--check works on the default agent's model connection; "
                    "a custom --app brings its own"
                )
            _warn_ignored_agent_flags(args)
            loaded_target = load_app_target(app_target)
            if isinstance(loaded_target, FastAPI):
                _warn_ignored_app_flags(args)
                built_app = loaded_target
                deps = getattr(built_app.state, "deps", None)
                custom_agents = (
                    list(deps.agents.values()) if isinstance(deps, RouterDeps) else []
                )
                db_desc = "(configured by the app)"
            else:
                agent_or_agents = loaded_target
                custom_agents = (
                    list(agent_or_agents.values())
                    if isinstance(agent_or_agents, Mapping)
                    else [agent_or_agents]
                )
                # Without --db, create_app derives the file from the agent name.
                db_desc = db_path or "(./.lovia/<agent>.db, from the agent's name)"
            for custom_agent in custom_agents:
                _warn_if_exposed(host, custom_agent.workspace)
            summary = webconfig.format_app_summary(
                version=__version__, app_target=app_target, db_desc=db_desc, url=url
            )
        else:
            home = _home(args)
            loaded = webconfig.load_active(home)
            # The connection the default agent runs on — what --check probes
            # and the summary shows.
            profile = loaded.config.agent_profile(loaded.config.default_agent)
            conn = (
                webconfig.Connection.from_profile(profile)
                if profile is not None
                else webconfig.Connection()
            )
            if args.check:
                # Read-only diagnosis: report, probe, exit — never serves.
                return webconfig.run_check(conn, version=__version__, path=loaded.path)
            # It holds the chat history and memory beside the saved
            # secrets: owner-only, never committed.
            webconfig.protect_home(home)
            db_desc = db_path or str(home / "lovia.db")
            # Anything missing (no model, a keyless official host) means
            # "serve the setup screen": the browser's Settings is the one
            # write path, on a TTY exactly like everywhere else.
            unconfigured = bool(webconfig.ConfigRuntime.serveable(loaded.config))
            general = Deployment(home=home).general_workspace()
            deployment = Deployment(
                workspace=None if args.no_workspace else _launch_dir(args, general),
                readonly=args.readonly,
                lock_workspace=args.lock_workspace,
                home=home,
            )
            _warn_if_remote_capable(host, deployment)
            store = ChatStore.sqlite(db_desc)
            question_channel = HumanChannel()
            config_runtime = webconfig.ConfigRuntime(
                deployment=deployment,
                loaded=loaded,
                store=store,
                question_channel=question_channel,
            )
            if deployment.workspace is not None:
                roots = deployment.roots()
                workspaces = WorkspaceSwitch(
                    roots=roots,
                    modes=cast("tuple[WorkspaceMode, ...]", deployment.modes()),
                    build=config_runtime.workspace_for,
                    agent_for=config_runtime.agent_for,
                    general=general if roots == "any" else None,
                    remotes=deployment.remotes(),
                    agent_for_remote=config_runtime.agent_for_remote,
                )
            if unconfigured:
                agent_or_agents = {}
                summary = webconfig.format_unconfigured_summary(
                    version=__version__, url=url, db_desc=db_desc, path=loaded.path
                )
            else:
                # The same build hot-reload uses, so boot and Settings can't
                # drift. Eager construction also surfaces vendor-prefix typos
                # at startup instead of on the first chat message.
                try:
                    agent_or_agents, aux_model = config_runtime.build()
                except (UserError, ValueError) as exc:
                    raise CliError(str(exc)) from exc
                summary = webconfig.format_summary(
                    conn,
                    version=__version__,
                    url=url,
                    config_desc=(
                        loaded.label if loaded.exists else "(not saved — session only)"
                    ),
                    agents_desc=_agents_desc(loaded.config),
                    workspace_desc=_workspace_desc(deployment, loaded.config),
                    db_desc=db_desc,
                )
            max_turns = config_runtime.max_turns()
            followups = config_runtime.followups

        # stdout, not the logger: the summary must be visible at every log
        # level, while log lines keep flowing to stderr.
        print(summary, flush=True)
        if built_app is None:
            built_app = create_app(
                agent_or_agents,
                title=title,
                # `store` wins when set (default agent); otherwise create_app
                # builds one from db_path (the custom --app path).
                store=store,
                db_path=db_path,
                max_turns=max_turns,
                token=token or _generated_token(host),
                # On, unlike create_app's default: the bundled CLI is a
                # finished chat app where the chips are part of it (each
                # configured agent may turn them off).
                followups=followups,
                title_model=aux_model,
                followup_model=aux_model,
                # Deny an unanswered tool approval after 10 minutes. Without it
                # a clientless run (a scheduled fire, a subagent task) parked on
                # an approval holds one of the concurrency slots forever; for a
                # chat the user is watching, ten idle minutes means the answer
                # is no.
                approval_timeout=600,
                # Same policy for the ask_human tool: an unanswered question is
                # cancelled after 10 minutes (the model gets a tool error and
                # continues), so a scheduled run that asks can never park.
                question_channel=question_channel,
                question_timeout=600,
                config_runtime=config_runtime,
                workspaces=workspaces,
            )
        serve(
            built_app,
            host=host,
            port=port,
            log_level=level.lower(),
            root_path=root_path,
        )
    except UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
