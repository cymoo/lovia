"""``lovia web`` (or ``python -m lovia.web``) — launch the lovia chat UI.

Builds a ready-made agent (model, skills, long-term memory, a todo checklist,
current-date awareness, model-driven scheduled runs, built-in tools — time,
HTTP fetch, web search — and a workspace) and serves it with the bundled web
UI. ``--app module:attribute`` serves your own ``Agent`` instead.

The model connection is not a flag: it lives in ``config.json`` (see
:mod:`lovia.web.config`), created by the first-run wizard / ``--setup`` and
edited in the web UI's Settings. The user-facing contract is the ``--help``
text: :data:`DESCRIPTION`, :data:`EPILOG` and the option groups in
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
from ..agent import Agent
from ..context import Compaction, ContextPolicy
from ..exceptions import UserError
from ..http_config import DEFAULT_TIMEOUT
from ..log_config import enable_logging
from ..providers import provider_from_string
from ..reliability import RetryPolicy
from ..tools import HumanChannel
from . import config as webconfig
from .app import _default_db_path, _display_host, serve
from .auth import is_loopback
from .builder import (
    DEFAULT_MAX_TURNS,
    DEFAULT_MEMORY_DIR,
    DEFAULT_RETRIES,
    DEFAULT_SKILLS_DIR,
    DEFAULT_WORKSPACE_MODE,
    INSTRUCTIONS_FILES,
    _first,
    _mode_flag,
    build_default_agent,
    resolve_followup_model,
    resolve_followups,
    resolve_max_retries,
    resolve_max_turns,
)
from .store import ChatStore

log = logging.getLogger("lovia.web.cli")

LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

DESCRIPTION = """\
Launch the lovia chat UI in your browser.

Serves a ready-made agent — skills, long-term memory, todos, web search,
scheduled runs, and a workspace rooted at the current directory. The model
connection is asked once on first run, checked against the endpoint, and
saved to config.json so it is never retyped.
"""

EPILOG = """\
examples:
  lovia web                                # first run opens the setup wizard
  lovia web --setup                        # change the saved model or key
  lovia web --port 9000
  lovia web --workspace ~/notes --readonly # let it read, not write
  lovia web --app myagents:assistant       # serve your own agent

configuration:
  The model connection (model, base URL, API key, context window), extra
  models, web search and role assignments live in ./.lovia/config.json —
  the project file wins — or ~/.lovia/config.json (any directory). Managed
  by --setup and the web UI's Settings; inspect with --check. The server and
  agent options above stay flags, each with the LOVIA_* variable shown.
"""


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
        "model", "the connection lives in config.json — these manage it"
    )
    model.add_argument(
        "--setup",
        action="store_true",
        help="rerun the connection wizard — change the saved model, endpoint "
        "or key (Enter keeps a value) — then launch",
    )
    model.add_argument(
        "--check",
        action="store_true",
        help="print the configured connection, probe the endpoint, and exit "
        "(0 reachable, 2 not)",
    )

    agent = p.add_argument_group(
        "agent", "the default agent; --app serves your own instead, ignoring the rest"
    )
    agent.add_argument(
        "--app",
        metavar="MODULE:ATTR",
        help="serve your own Agent (or mapping/factory) instead (env LOVIA_APP)",
    )
    agent.add_argument(
        "--instructions-file",
        metavar="FILE",
        help="system prompt file (env LOVIA_INSTRUCTIONS_FILE; "
        f"default: ./{'/'.join(INSTRUCTIONS_FILES)} if present)",
    )
    agent.add_argument(
        "--instructions",
        metavar="TEXT",
        help="system prompt text, overriding --instructions-file",
    )
    agent.add_argument(
        "--skills-dir",
        action="append",
        metavar="DIR",
        help="skill directory; repeatable (env LOVIA_SKILLS_DIR; "
        f"default ./{DEFAULT_SKILLS_DIR} if present)",
    )
    agent.add_argument(
        "--memory-dir",
        metavar="DIR",
        help="long-term memory directory, created if missing "
        f"(env LOVIA_MEMORY_DIR, default {DEFAULT_MEMORY_DIR})",
    )
    agent.add_argument(
        "--no-memory", action="store_true", help="disable long-term memory"
    )
    agent.add_argument(
        "--no-subagents",
        action="store_true",
        help="disable background subagents (spawn_subagent etc.)",
    )
    agent.add_argument(
        "--workspace",
        metavar="DIR",
        help="root the agent can read/edit/run in (env LOVIA_WORKSPACE, default .); "
        f"mode defaults to {DEFAULT_WORKSPACE_MODE} — writes inside the root, "
        "shell asks first",
    )
    ws = agent.add_mutually_exclusive_group()
    ws.add_argument(
        "--readonly",
        action="store_true",
        help="reads inside the root only — no writes, no shell "
        "(LOVIA_WORKSPACE_MODE=readonly)",
    )
    ws.add_argument(
        "--trusted",
        action="store_true",
        help="shell and reads outside the root, unprompted "
        "(LOVIA_WORKSPACE_MODE=trusted)",
    )
    ws.add_argument(
        "--no-workspace", action="store_true", help="no filesystem/shell workspace"
    )

    server = p.add_argument_group("server")
    server.add_argument(
        "--host", help="bind address (env LOVIA_HOST, default 127.0.0.1)"
    )
    server.add_argument(
        "--port", type=int, help="port to listen on (env LOVIA_PORT, default 8000)"
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
        "--no-followups",
        action="store_true",
        help="don't suggest follow-up questions after a reply (env LOVIA_FOLLOWUPS=0)",
    )
    server.add_argument(
        "--db",
        metavar="FILE",
        help="SQLite file for chat history (env LOVIA_DB, default ./.lovia/<agent>.db)",
    )

    advanced = p.add_argument_group("advanced")
    advanced.add_argument(
        "--max-turns",
        type=int,
        metavar="N",
        help="max agent turns per message (env LOVIA_MAX_TURNS, "
        f"default {DEFAULT_MAX_TURNS})",
    )
    advanced.add_argument(
        "--max-tokens",
        type=int,
        metavar="N",
        help="max output tokens per response (env LOVIA_MAX_TOKENS, "
        "default: the provider's)",
    )
    advanced.add_argument(
        "--max-retries",
        type=int,
        metavar="N",
        help="attempts after the first on transient provider errors "
        f"(env LOVIA_MAX_RETRIES, default {DEFAULT_RETRIES} retries; 0 disables)",
    )
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


def load_app_target(target: str) -> Agent[Any] | Mapping[str, Agent[Any]]:
    """Import ``module:attribute`` and return the Agent (or mapping) it names.

    If the attribute is a callable that is not itself an Agent/mapping, it is
    treated as a factory and called with no arguments.
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
    if callable(obj) and not isinstance(obj, (Agent, Mapping)):
        obj = obj()
    if not isinstance(obj, (Agent, Mapping)):
        raise CliError(
            f"--app target {target!r} is not an Agent or a mapping of agents "
            f"(got {type(obj).__name__})"
        )
    return cast("Agent[Any] | Mapping[str, Agent[Any]]", obj)


def _warn_ignored_agent_flags(args: argparse.Namespace) -> None:
    flags = [
        ("--skills-dir", bool(args.skills_dir)),
        ("--memory-dir", args.memory_dir is not None),
        ("--no-memory", args.no_memory),
        ("--no-subagents", args.no_subagents),
        ("--workspace", args.workspace is not None),
        ("--readonly", args.readonly),
        ("--trusted", args.trusted),
        ("--no-workspace", args.no_workspace),
        ("--instructions", args.instructions is not None),
        ("--instructions-file", args.instructions_file is not None),
        ("--max-tokens", args.max_tokens is not None),
    ]
    ignored = [name for name, given in flags if given]
    if ignored:
        log.warning("--app set; ignoring default-agent options: %s", ", ".join(ignored))


def _warn_if_exposed(host: str, workspace: object) -> None:
    """Warn when a write/shell-capable workspace is reachable off-host.

    Non-loopback binds are always token-guarded (``serve`` generates one when
    none is given), but a leaked or shared token then grants file edits and
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


def _workspace_desc(args: argparse.Namespace, workspace: object) -> str:
    """Human line for the startup summary, e.g. ``/path/to/dir (coding)``."""
    if workspace is None:
        return "(none)"
    mode = (
        _first(_mode_flag(args), os.getenv("LOVIA_WORKSPACE_MODE"))
        or DEFAULT_WORKSPACE_MODE
    )
    root = getattr(workspace, "root", ".")
    return f"{Path(root).resolve()} ({mode})"


def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    args = build_parser(prog).parse_args(argv)
    try:
        level = (_first(args.log_level, os.getenv("LOVIA_LOG_LEVEL")) or "info").upper()
        if level not in LOG_LEVELS:
            raise CliError(
                f"invalid log level: {level!r}",
                hint=f"choose one of: {', '.join(lv.lower() for lv in LOG_LEVELS)}",
            )
        # %(run_source)s tags every line emitted inside a supervised run's
        # task with what started it — [user] / [schedule:<id>] /
        # [subagent:<session>] — so parallel background work stays legible.
        enable_logging(
            level,
            format="%(asctime)s %(levelname)-7s %(name)s: %(run_source)s%(message)s",
        )
        from .supervisor import run_source_log_filter

        for handler in logging.getLogger("lovia").handlers:
            handler.addFilter(run_source_log_filter())

        if args.provider_timeout is not None:
            if args.provider_timeout <= 0:
                raise CliError(
                    f"--provider-timeout must be > 0, got {args.provider_timeout}"
                )
            # The providers read these when constructing their HTTP client.
            os.environ["LOVIA_PROVIDER_TIMEOUT"] = str(args.provider_timeout)
        if args.trust_env:
            os.environ["LOVIA_PROVIDER_TRUST_ENV"] = "1"
        # None = no override: the agent's own retry posture applies.
        max_retries = resolve_max_retries(args.max_retries)
        retry = (
            RetryPolicy(max_attempts=max_retries + 1)
            if max_retries is not None
            else None
        )

        host = _first(args.host, os.getenv("LOVIA_HOST")) or "127.0.0.1"
        port = args.port if args.port is not None else _env_int("LOVIA_PORT", 8000)
        title = _first(args.title, os.getenv("LOVIA_TITLE")) or "lovia"
        db_path = _first(args.db, os.getenv("LOVIA_DB"))
        # None on a non-loopback bind → serve() generates and prints one.
        token = _first(args.token, os.getenv("LOVIA_WEB_TOKEN"))
        # A wildcard bind is not a browsable address: show one that is.
        url = f"http://{_display_host(host)}:{port}"

        agent_or_agents: Agent[Any] | Mapping[str, Agent[Any]]
        # For the default agent we build the store up front (rather than letting
        # create_app build it) so the schedule_run tool can close over the same
        # ChatStore the scheduler polls. Custom --app agents keep using db_path.
        store: ChatStore | None = None
        # Compaction policy for the default agent; None lets create_app pick its
        # own default for a custom --app agent.
        context_policy: ContextPolicy | None = None
        # ask_human bridge for the default agent. A custom --app brings its own
        # agents; wiring their channel is the library API's job (create_app's
        # question_channel=), so it stays None here.
        question_channel: HumanChannel | None = None
        followup_model = None
        app_target = _first(args.app, os.getenv("LOVIA_APP"))
        if app_target:
            for flag, given in (("--setup", args.setup), ("--check", args.check)):
                if given:
                    raise CliError(
                        f"{flag} works on the default agent's model connection; "
                        "a custom --app brings its own"
                    )
            _warn_ignored_agent_flags(args)
            agent_or_agents = load_app_target(app_target)
            custom_agents = (
                agent_or_agents.values()
                if isinstance(agent_or_agents, Mapping)
                else [agent_or_agents]
            )
            for custom_agent in custom_agents:
                _warn_if_exposed(host, custom_agent.workspace)
            summary = webconfig.format_app_summary(
                version=__version__,
                app_target=app_target,
                # Without --db, create_app derives the file from the agent name.
                db_desc=db_path or "(./.lovia/<agent>.db, from the agent's name)",
                url=url,
            )
        else:
            db_desc = db_path or str(_default_db_path("lovia"))
            loaded = webconfig.load_active()
            profile = loaded.config.default_profile()
            conn = (
                webconfig.Connection.from_profile(profile)
                if profile is not None
                else webconfig.Connection()
            )
            if args.check:
                # Read-only diagnosis: report, probe, exit — never the wizard.
                return webconfig.run_check(conn, version=__version__)
            if conn.missing() or args.setup:
                if not sys.stdin.isatty():
                    missing = conn.missing()
                    if missing:
                        raise CliError(
                            f"no {' or '.join(missing)} configured",
                            hint=webconfig.CONFIG_HINT,
                        )
                    raise CliError("--setup needs an interactive terminal")
                conn = webconfig.interactive_setup(loaded, reconfigure=args.setup)
            assert conn.model is not None
            # The wizard upserted the entered profile into the config (even on
            # a declined save), so roles resolve against the session's truth.
            active_profile = loaded.config.default_profile()
            try:
                provider = provider_from_string(
                    conn.model,
                    api_key=conn.api_key,
                    base_url=conn.base_url,
                    supports_vision=(
                        active_profile.vision_override() if active_profile else None
                    ),
                )
            except ValueError as exc:
                # Eager construction also surfaces vendor-prefix typos at
                # startup instead of on the first chat message.
                raise CliError(str(exc)) from exc
            # The store is created only after setup succeeds so an aborted
            # first-run wizard leaves no stray database behind.
            store = ChatStore.sqlite(db_desc)
            question_channel = HumanChannel()
            agent = build_default_agent(
                args,
                store,
                provider,
                question_channel=question_channel,
                config=loaded.config,
            )
            followup_model = resolve_followup_model(loaded.config.aux_profile())
            context_policy = Compaction(context_window=conn.context_window)
            _warn_if_exposed(host, agent.workspace)
            summary = webconfig.format_summary(
                conn,
                version=__version__,
                url=url,
                config_desc=(
                    loaded.label if loaded.exists else "(not saved — session only)"
                ),
                workspace_desc=_workspace_desc(args, agent.workspace),
                db_desc=db_desc,
            )
            agent_or_agents = agent

        # stdout, not the logger: the summary must be visible at every log
        # level, while log lines keep flowing to stderr.
        print(summary, flush=True)
        serve(
            agent_or_agents,
            host=host,
            port=port,
            title=title,
            # `store` wins when set (default agent); otherwise create_app builds
            # one from db_path (the custom --app path).
            store=store,
            db_path=db_path,
            context_policy=context_policy,
            max_turns=resolve_max_turns(args.max_turns),
            retry=retry,
            token=token,
            followups=resolve_followups(args.no_followups),
            followup_model=followup_model,
            # Deny an unanswered tool approval after 10 minutes. Without it a
            # clientless run (a scheduled fire, a subagent task) parked on an
            # approval holds one of the concurrency slots forever; for a chat
            # the user is watching, ten idle minutes means the answer is no.
            approval_timeout=600,
            # Same policy for the ask_human tool: an unanswered question is
            # cancelled after 10 minutes (the model gets a tool error and
            # continues), so a scheduled run that asks can never park forever.
            question_channel=question_channel,
            question_timeout=600,
            log_level=level.lower(),
        )
    except UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
