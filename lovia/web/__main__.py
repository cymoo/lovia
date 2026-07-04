"""``python -m lovia.web`` — launch the lovia chat UI from the command line.

Builds a sensible default agent (a model, skills, long-term memory, a todo
checklist, current-date awareness, model-driven scheduled runs, built-in tools
— time, HTTP fetch, web search — and a workspace, all configurable via flags or
``LOVIA_*`` environment variables) and serves it with the bundled web UI. Point ``--app
module:attribute`` at your own ``Agent`` to serve that instead.

Examples::

    python -m lovia.web                           # default agent, ./skills, cwd workspace
    python -m lovia.web --port 9000 --model openai:gpt-5.5
    python -m lovia.web --skills-dir ./skills --skills-dir ./team-skills
    python -m lovia.web --memory-dir ./mem        # persist memory under ./mem
    python -m lovia.web --no-memory               # disable long-term memory
    python -m lovia.web --app myagents:assistant  # serve your own agent

Common options also read ``LOVIA_*`` env vars. If ``python-dotenv`` is
installed, a ``.env`` file in the current directory (or ``--env-file``) is
loaded first; without it, ``.env`` files are skipped (no hard dependency).
Precedence is: command-line flag > environment variable > built-in default.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import Any, cast, get_args

from .. import __version__
from ..agent import Agent
from ..context import Compaction, ContextPolicy
from ..exceptions import UserError
from ..log_config import enable_logging
from ..plugins import Memory, Plugin, Skills, Todo
from ..providers import ModelSettings, model_from_env, provider_from_string
from ..providers.base import context_window as provider_context_window
from ..reliability import RetryPolicy
from ..tools import Tool, current_date, duckduckgo_search, http_fetch, now
from ..workspace import LocalWorkspace, Workspace, WorkspaceMode
from .app import DEFAULT_CONTEXT_WINDOW, serve
from .scheduling import Scheduling
from .store import ChatStore

log = logging.getLogger("lovia.web.cli")

WORKSPACE_MODES: tuple[str, ...] = get_args(WorkspaceMode)
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
INSTRUCTIONS_FILES: tuple[str, ...] = ("AGENTS.md",)
DEFAULT_SKILLS_DIR = "skills"
DEFAULT_MEMORY_DIR = "./.lovia/memory"
DEFAULT_AGENT_NAME = "lovia"
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_TURNS = 50
GENERIC_INSTRUCTIONS = (
    "You are a helpful assistant running in the lovia web UI. "
    "Be concise and accurate, and use your tools and skills when they help."
)


class CliError(UserError):
    """A user-facing CLI misconfiguration; rendered without a traceback."""


def _first(*values: str | None) -> str | None:
    """Return the first non-empty value (the precedence helper)."""
    for value in values:
        if value:
            return value
    return None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise CliError(f"invalid integer for {name}: {raw!r}") from exc


def _env_int_optional(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise CliError(f"invalid integer for {name}: {raw!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m lovia.web",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"lovia {__version__}")
    p.add_argument("--host", help="bind address (env LOVIA_HOST, default 127.0.0.1)")
    p.add_argument(
        "--port", type=int, help="port to listen on (env LOVIA_PORT, default 8000)"
    )
    p.add_argument(
        "--db",
        metavar="FILE",
        help="SQLite file for chat persistence "
        "(env LOVIA_DB, default <agent>.db in cwd)",
    )
    p.add_argument(
        "--model",
        help="model id, e.g. openai:gpt-5.5 (env LOVIA_MODEL, then "
        "OPENAI_DEFAULT_MODEL / ANTHROPIC_DEFAULT_MODEL)",
    )
    p.add_argument(
        "--skills-dir",
        action="append",
        metavar="DIR",
        help="skill directory; repeatable (env LOVIA_SKILLS_DIR; "
        f"default ./{DEFAULT_SKILLS_DIR} if present)",
    )
    p.add_argument(
        "--memory-dir",
        metavar="DIR",
        help="directory for long-term memory (notes + searchable archive), "
        f"created if missing (env LOVIA_MEMORY_DIR, default {DEFAULT_MEMORY_DIR})",
    )
    p.add_argument(
        "--no-memory",
        action="store_true",
        help="disable the long-term memory plugin (on by default)",
    )
    p.add_argument(
        "--workspace",
        metavar="DIR",
        help="workspace root the agent can read/edit/run in "
        "(env LOVIA_WORKSPACE, default .)",
    )
    p.add_argument(
        "--workspace-mode",
        choices=WORKSPACE_MODES,
        help=f"workspace permissions: {', '.join(WORKSPACE_MODES)} "
        "(env LOVIA_WORKSPACE_MODE, default trusted)",
    )
    p.add_argument(
        "--no-workspace",
        action="store_true",
        help="give the agent no filesystem/shell workspace",
    )
    p.add_argument(
        "--instructions",
        metavar="TEXT",
        help="system prompt text (overrides --instructions-file and auto-load)",
    )
    p.add_argument(
        "--instructions-file",
        metavar="FILE",
        help="read the system prompt from FILE (env LOVIA_INSTRUCTIONS_FILE; "
        f"else auto-loads {'/'.join(INSTRUCTIONS_FILES)} from cwd, else generic)",
    )
    p.add_argument(
        "--app",
        metavar="MODULE:ATTR",
        help="serve your own Agent (or mapping/factory) instead of the default "
        "agent; default-agent flags are then ignored (env LOVIA_APP)",
    )
    p.add_argument(
        "--env-file",
        action="append",
        metavar="FILE",
        help="load environment from FILE via python-dotenv; repeatable "
        "(default ./.env if present)",
    )
    p.add_argument("--title", help="web UI title (env LOVIA_TITLE, default lovia)")
    p.add_argument(
        "--log-level",
        metavar="LEVEL",
        help="logging level: debug/info/warning/error (env LOVIA_LOG_LEVEL, "
        "default info)",
    )
    p.add_argument(
        "--max-retries",
        type=int,
        metavar="N",
        help="provider retry attempts after the first on transient errors "
        "(env LOVIA_MAX_RETRIES, default 2; 0 disables)",
    )
    p.add_argument(
        "--provider-timeout",
        type=float,
        metavar="SECONDS",
        help="per-request model provider timeout in seconds "
        "(env LOVIA_PROVIDER_TIMEOUT, default 60)",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        metavar="N",
        help="max output tokens per model response "
        "(env LOVIA_MAX_TOKENS, default: provider default)",
    )
    p.add_argument(
        "--context-window",
        type=int,
        metavar="N",
        help="model context window in tokens used for compaction "
        "(env LOVIA_CONTEXT_WINDOW, default: auto-detect, else 200K)",
    )
    p.add_argument(
        "--max-turns",
        type=int,
        metavar="N",
        help="max agent turns per request (env LOVIA_MAX_TURNS, default 50)",
    )
    p.add_argument(
        "--trust-env",
        action="store_true",
        help="let model provider HTTP clients honor HTTP(S)_PROXY / NO_PROXY "
        "env vars (env LOVIA_PROVIDER_TRUST_ENV)",
    )
    return p


def load_env_files(env_files: list[str] | None) -> None:
    """Load ``.env`` files into ``os.environ`` if python-dotenv is available.

    Existing environment variables win over file values (``override=False``).
    A missing python-dotenv is fatal only when ``--env-file`` was given
    explicitly; otherwise auto-loading is silently skipped.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        if env_files:
            raise CliError(
                "--env-file requires python-dotenv, which is not installed.",
                hint="Install it with: pip install python-dotenv",
            )
        log.debug("python-dotenv not installed; skipping .env autoload")
        return

    if env_files:
        for raw in env_files:
            path = Path(raw)
            if not path.is_file():
                raise CliError(f"env file not found: {path}")
            load_dotenv(path, override=False)
            log.debug("loaded env file %s", path)
    else:
        default = Path(".env")
        if default.is_file():
            load_dotenv(default, override=False)
            log.debug("loaded env file %s", default)


def resolve_model(cli_model: str | None) -> str:
    model = cli_model or model_from_env(required=False)
    if not model:
        raise CliError(
            "no model configured.",
            hint="pass --model (e.g. openai:gpt-5.5) or set LOVIA_MODEL / "
            "OPENAI_DEFAULT_MODEL.",
        )
    return model


def resolve_max_retries(cli: int | None) -> int:
    """Provider retry attempts after the first (flag > LOVIA_MAX_RETRIES > 2)."""
    value = (
        cli if cli is not None else _env_int("LOVIA_MAX_RETRIES", DEFAULT_MAX_RETRIES)
    )
    if value < 0:
        raise CliError(f"--max-retries must be >= 0, got {value}")
    return value


def resolve_max_turns(cli: int | None) -> int:
    """Per-run agent turn cap (flag > LOVIA_MAX_TURNS > 50)."""
    value = cli if cli is not None else _env_int("LOVIA_MAX_TURNS", DEFAULT_MAX_TURNS)
    if value < 1:
        raise CliError(f"--max-turns must be >= 1, got {value}")
    return value


def resolve_max_tokens(cli: int | None) -> int | None:
    """Max output tokens per response (flag > LOVIA_MAX_TOKENS > provider default)."""
    value = cli if cli is not None else _env_int_optional("LOVIA_MAX_TOKENS")
    if value is not None and value <= 0:
        raise CliError(f"--max-tokens must be > 0, got {value}")
    return value


def _detect_context_window(model: str) -> int | None:
    """Best-effort lookup of a model's context window; ``None`` if unknown."""
    try:
        provider = provider_from_string(model)
        return provider_context_window(provider, getattr(provider, "model", None))
    except Exception:  # noqa: BLE001 - detection must never break the launcher
        return None


def resolve_context_window(cli: int | None, model: str) -> int:
    """Compaction context window in tokens.

    Precedence: ``--context-window`` flag, ``LOVIA_CONTEXT_WINDOW``, the model's
    advertised window, then a 200K fallback for OpenAI-compatible endpoints not
    in the context-window table.
    """
    value = cli if cli is not None else _env_int_optional("LOVIA_CONTEXT_WINDOW")
    if value is not None:
        if value < 1:
            raise CliError(f"--context-window must be >= 1, got {value}")
        return value
    return _detect_context_window(model) or DEFAULT_CONTEXT_WINDOW


def resolve_context_policy(args: argparse.Namespace) -> ContextPolicy:
    """Compaction policy for the default agent (honors --context-window)."""
    window = resolve_context_window(args.context_window, resolve_model(args.model))
    return Compaction(context_window=window)


def resolve_skills_dirs(cli_dirs: list[str] | None) -> list[Path]:
    if cli_dirs:
        dirs = [Path(d) for d in cli_dirs]
        for d in dirs:
            if not d.is_dir():
                raise CliError(f"skills directory not found: {d}")
        return dirs
    env = os.getenv("LOVIA_SKILLS_DIR")
    if env:
        d = Path(env)
        if not d.is_dir():
            raise CliError(f"skills directory not found (LOVIA_SKILLS_DIR): {d}")
        return [d]
    default = Path(DEFAULT_SKILLS_DIR)
    return [default] if default.is_dir() else []


def resolve_memory(cli_dir: str | None, no_memory: bool) -> Memory | None:
    """Build the default :class:`Memory` plugin unless ``--no-memory`` is set.

    Storage-root precedence: ``--memory-dir`` > ``LOVIA_MEMORY_DIR`` >
    ``./.lovia/memory``. The directory need not exist yet — the notes file and
    archive db are created under it on first write.
    """
    if no_memory:
        return None
    root = _first(cli_dir, os.getenv("LOVIA_MEMORY_DIR")) or DEFAULT_MEMORY_DIR
    path = Path(root)
    if path.exists() and not path.is_dir():
        raise CliError(f"memory path is not a directory: {path}")
    log.info("memory enabled at %s", path)
    return Memory(root)


def resolve_tools() -> list[Tool]:
    """The always-on built-in tools for the default agent.

    ``now`` (current time) and ``http_fetch`` have no extra dependencies. Web
    search needs the optional ``ddgs`` backend (bundled with the ``web``/``ddg``
    extras); when it is missing we load the rest and log how to enable it rather
    than failing.
    """
    tools: list[Tool] = [now, http_fetch]
    try:
        tools.append(duckduckgo_search())
    except UserError:
        log.info(
            "web_search disabled: the 'ddgs' backend is not installed "
            "(pip install 'lovia[ddg]')."
        )
    return tools


def resolve_instructions(cli_text: str | None, cli_file: str | None) -> str:
    if cli_text is not None:
        return cli_text
    file = _first(cli_file, os.getenv("LOVIA_INSTRUCTIONS_FILE"))
    if file:
        path = Path(file)
        if not path.is_file():
            raise CliError(f"instructions file not found: {path}")
        return path.read_text(encoding="utf-8")
    for name in INSTRUCTIONS_FILES:
        path = Path(name)
        if path.is_file():
            log.info("using instructions from %s", path)
            return path.read_text(encoding="utf-8")
    return GENERIC_INSTRUCTIONS


def resolve_workspace(
    cli_dir: str | None, cli_mode: str | None, no_workspace: bool
) -> LocalWorkspace | None:
    if no_workspace:
        return None
    root = _first(cli_dir, os.getenv("LOVIA_WORKSPACE")) or "."
    mode = _first(cli_mode, os.getenv("LOVIA_WORKSPACE_MODE")) or "trusted"
    if mode not in WORKSPACE_MODES:
        raise CliError(
            f"invalid workspace mode: {mode!r}",
            hint=f"choose one of: {', '.join(WORKSPACE_MODES)}",
        )
    path = Path(root)
    if not path.is_dir():
        raise CliError(f"workspace directory not found: {path}")
    return Workspace.local(str(path), mode=cast(WorkspaceMode, mode))


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


def build_default_agent(args: argparse.Namespace, store: ChatStore) -> Agent[Any]:
    model = resolve_model(args.model)
    instructions = resolve_instructions(args.instructions, args.instructions_file)
    skills_dirs = resolve_skills_dirs(args.skills_dir)
    plugins: list[Plugin] = []
    if skills_dirs:
        plugins.append(Skills(*skills_dirs))
        log.info("loaded skills from %s", ", ".join(str(d) for d in skills_dirs))
    plugins.append(Todo())
    # Let the model create Scheduled runs from chat (gated by approval). Closes
    # over the app's store so it writes the rows the scheduler polls.
    plugins.append(Scheduling(store))
    memory = resolve_memory(args.memory_dir, args.no_memory)
    if memory is not None:
        plugins.append(memory)
    workspace = resolve_workspace(
        args.workspace, args.workspace_mode, args.no_workspace
    )
    agent: Agent[Any] = Agent(
        name=DEFAULT_AGENT_NAME,
        instructions=instructions,
        model=model,
        settings=ModelSettings(max_tokens=resolve_max_tokens(args.max_tokens)),
        plugins=plugins,
        tools=resolve_tools(),
        workspace=workspace,
    )
    # Tell the model today's date up front so it searches the current year and
    # skips the now->web_search round-trip. Date only (server-local tz): stable
    # within a prompt-cache window; precise time stays the `now` tool's job.
    agent.instruction(current_date())
    return agent


def _warn_ignored_agent_flags(args: argparse.Namespace) -> None:
    flags = [
        ("--model", args.model is not None),
        ("--skills-dir", bool(args.skills_dir)),
        ("--memory-dir", args.memory_dir is not None),
        ("--no-memory", args.no_memory),
        ("--workspace", args.workspace is not None),
        ("--workspace-mode", args.workspace_mode is not None),
        ("--no-workspace", args.no_workspace),
        ("--instructions", args.instructions is not None),
        ("--instructions-file", args.instructions_file is not None),
        ("--max-tokens", args.max_tokens is not None),
        ("--context-window", args.context_window is not None),
    ]
    ignored = [name for name, given in flags if given]
    if ignored:
        log.warning("--app set; ignoring default-agent options: %s", ", ".join(ignored))


def _is_loopback(host: str) -> bool:
    # NB: "::" and "0.0.0.0" are wildcards (all interfaces), NOT loopback.
    return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")


def _warn_if_exposed(host: str, workspace: object) -> None:
    """Warn when a write/shell-capable workspace is reachable off-host.

    Binding to a non-loopback address with the default trusted workspace lets
    anyone who can reach the port make the agent run shell or edit files.
    """
    policy = getattr(workspace, "policy", None)
    if policy is None or _is_loopback(host):
        return
    write_capable = (
        getattr(policy, "write", "deny") != "deny"
        or getattr(policy, "write_outside", "deny") != "deny"
    )
    if getattr(policy, "allow_shell", False) or write_capable:
        log.warning(
            "binding to non-loopback host %r with a write/shell-capable workspace: "
            "anyone who can reach this port can make the agent edit files or run "
            "shell commands. Use --workspace-mode readonly, --no-workspace, or bind "
            "to 127.0.0.1.",
            host,
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        load_env_files(args.env_file)
        level = (_first(args.log_level, os.getenv("LOVIA_LOG_LEVEL")) or "info").upper()
        if level not in LOG_LEVELS:
            raise CliError(
                f"invalid log level: {level!r}",
                hint=f"choose one of: {', '.join(lv.lower() for lv in LOG_LEVELS)}",
            )
        enable_logging(level)

        if args.provider_timeout is not None:
            if args.provider_timeout <= 0:
                raise CliError(
                    f"--provider-timeout must be > 0, got {args.provider_timeout}"
                )
            # The providers read these when constructing their HTTP client.
            os.environ["LOVIA_PROVIDER_TIMEOUT"] = str(args.provider_timeout)
        if args.trust_env:
            os.environ["LOVIA_PROVIDER_TRUST_ENV"] = "1"
        retry = RetryPolicy(max_attempts=resolve_max_retries(args.max_retries) + 1)

        host = _first(args.host, os.getenv("LOVIA_HOST")) or "127.0.0.1"
        port = args.port if args.port is not None else _env_int("LOVIA_PORT", 8000)
        title = _first(args.title, os.getenv("LOVIA_TITLE")) or "lovia"
        db_path = _first(args.db, os.getenv("LOVIA_DB"))

        agent_or_agents: Agent[Any] | Mapping[str, Agent[Any]]
        # For the default agent we build the store up front (rather than letting
        # create_app build it) so the schedule_run tool can close over the same
        # ChatStore the scheduler polls. Custom --app agents keep using db_path.
        store: ChatStore | None = None
        # Compaction policy for the default agent; None lets create_app pick its
        # own default for a custom --app agent.
        context_policy: ContextPolicy | None = None
        app_target = _first(args.app, os.getenv("LOVIA_APP"))
        if app_target:
            _warn_ignored_agent_flags(args)
            agent_or_agents = load_app_target(app_target)
        else:
            store = ChatStore.sqlite(db_path or f"{DEFAULT_AGENT_NAME}.db")
            agent = build_default_agent(args, store)
            context_policy = resolve_context_policy(args)
            _warn_if_exposed(host, agent.workspace)
            agent_or_agents = agent

        log.info("serving lovia on http://%s:%d", host, port)
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
