"""``lovia web`` (or ``python -m lovia.web``) — launch the lovia chat UI.

Builds a sensible default agent (a model, skills, long-term memory, a todo
checklist, current-date awareness, model-driven scheduled runs, built-in tools
— time, HTTP fetch, web search — and a workspace, all configurable via flags or
``LOVIA_*`` environment variables) and serves it with the bundled web UI. Point ``--app
module:attribute`` at your own ``Agent`` to serve that instead.

Examples::

    lovia web                                # default agent, ./skills, cwd workspace
    lovia web --port 9000 --model openai:gpt-5.5
    lovia web --model deepseek-v4-pro --base-url https://api.deepseek.com
    lovia web --skills-dir ./skills --skills-dir ./team-skills
    lovia web --memory-dir ./mem             # persist memory under ./mem
    lovia web --no-memory                    # disable long-term memory
    lovia web --app myagents:assistant       # serve your own agent

First run: whatever required configuration is missing (the model; an API key
when the endpoint is the official OpenAI/Anthropic API) is asked
interactively, validated against the endpoint, and can be saved to
``.lovia/config.env`` (owner-only, git-ignored) so it is never retyped.

Configuration precedence: command-line flag > environment variable >
``.lovia/config.env`` (or ``--env-file``). The model endpoint uses the provider's
standard variables — ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` or
``ANTHROPIC_*``, chosen by the model's vendor prefix — while everything else
uses ``LOVIA_*``.
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
from ..http_config import DEFAULT_TIMEOUT
from ..log_config import enable_logging
from ..plugins import Memory, Plugin, Skills, Todo
from ..providers import (
    ModelSettings,
    Provider,
    provider_from_string,
    supports_vision,
)
from ..reliability import RetryPolicy
from ..tools import (
    Tool,
    current_date,
    duckduckgo_search,
    http_fetch,
    now,
    tavily_search,
)
from ..workspace import LocalWorkspace, Workspace, WorkspaceMode
from . import setup
from .app import _default_db_path, serve
from .auth import is_loopback
from .scheduling import Scheduling
from .store import ChatStore
from .vision import make_see_image_tool

log = logging.getLogger("lovia.web.cli")

WORKSPACE_MODES: tuple[str, ...] = get_args(WorkspaceMode)
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
INSTRUCTIONS_FILES: tuple[str, ...] = ("AGENTS.md",)
DEFAULT_SKILLS_DIR = "skills"
DEFAULT_MEMORY_DIR = "./.lovia/memory"
DEFAULT_AGENT_NAME = "lovia"
DEFAULT_MAX_TURNS = 50
# Rendered into --help from the core defaults so the text can never drift.
DEFAULT_RETRIES = RetryPolicy().max_attempts - 1
# Match the core library's ``Workspace.local`` default: shell and out-of-root
# reads go through human approval. ``trusted`` (unprompted shell, read
# anywhere) stays available via --trusted / LOVIA_WORKSPACE_MODE.
DEFAULT_WORKSPACE_MODE = "coding"
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


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog or "python -m lovia.web",
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
        "(env LOVIA_DB, default ./.lovia/<agent>.db)",
    )
    p.add_argument(
        "--model",
        help="model id, e.g. openai:gpt-5.5 (env LOVIA_MODEL)",
    )
    p.add_argument(
        "--base-url",
        metavar="URL",
        help="model API base URL for the provider chosen by the model's "
        "vendor prefix (env OPENAI_BASE_URL / ANTHROPIC_BASE_URL)",
    )
    p.add_argument(
        "--api-key",
        metavar="KEY",
        help="model API key for the provider chosen by the model's vendor "
        "prefix (env OPENAI_API_KEY / ANTHROPIC_API_KEY; prefer the env or "
        "the first-run prompt — flags are visible in the process list)",
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
    ws = p.add_mutually_exclusive_group()
    ws.add_argument(
        "--readonly",
        action="store_true",
        help="workspace can only read files inside its root — no writes, no "
        f"shell (default mode: {DEFAULT_WORKSPACE_MODE} — writes in the root "
        "allowed, shell asks first; env LOVIA_WORKSPACE_MODE)",
    )
    ws.add_argument(
        "--trusted",
        action="store_true",
        help="workspace runs shell commands and reads outside its root "
        "without asking (env LOVIA_WORKSPACE_MODE)",
    )
    ws.add_argument(
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
        "(defaults: .lovia/config.env then ./.env, if present)",
    )
    p.add_argument(
        "--token",
        metavar="TOKEN",
        help="API auth token; loopback binds don't need one, non-loopback "
        "binds get one generated and printed when omitted (env "
        "LOVIA_WEB_TOKEN; prefer the env — flags are visible in the "
        "process list)",
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
        "(env LOVIA_MAX_RETRIES; default: the agent's retry posture, "
        f"{DEFAULT_RETRIES} retries; 0 disables)",
    )
    p.add_argument(
        "--provider-timeout",
        type=float,
        metavar="SECONDS",
        help="per-request model provider timeout in seconds "
        f"(env LOVIA_PROVIDER_TIMEOUT, default {DEFAULT_TIMEOUT:g})",
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
        "(env LOVIA_CONTEXT_WINDOW; default: ask the provider, reactive "
        "overflow handling when unknown)",
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


def load_env_files(env_files: list[str] | None) -> dict[str, str]:
    """Load the ``--env-file`` files (or the autoload defaults); report added keys.

    Default autoload is ``.lovia/config.env`` (where the setup wizard saves),
    then a legacy ``./.env`` for back-compat. The process environment always
    wins because files never override existing variables (``override=False``),
    and the canonical file wins over the legacy one (it loads first). Returns
    ``{key: <file name>}`` for keys the files introduced — the startup summary
    shows it as each value's source.

    A missing python-dotenv is fatal only when ``--env-file`` was given
    explicitly; otherwise auto-loading is silently skipped. This lives in the
    CLI only — the embeddable ``create_app`` / ``serve`` never loads env files.
    """
    sources: dict[str, str] = {}
    try:
        from dotenv import load_dotenv
    except ImportError:
        if env_files:
            raise CliError(
                "--env-file requires python-dotenv, which is not installed.",
                hint="Install it with: pip install python-dotenv",
            )
        log.debug("python-dotenv not installed; skipping .env autoload")
        return sources

    def load(path: Path) -> None:
        before = set(os.environ)
        load_dotenv(path, override=False)
        for key in os.environ.keys() - before:
            sources[key] = path.name
        log.debug("loaded env file %s", path)

    if env_files:
        for raw in env_files:
            path = Path(raw)
            if not path.is_file():
                raise CliError(f"env file not found: {path}")
            load(path)
    else:
        # Canonical first (wins on conflicts via override=False), then legacy.
        for default in (setup.config_path(), Path(".env")):
            if default.is_file():
                load(default)
    return sources


def resolve_max_retries(cli: int | None) -> int | None:
    """Explicit provider retry count, or ``None`` for the agent's posture.

    Precedence: ``--max-retries`` flag, then ``LOVIA_MAX_RETRIES``. ``None``
    means no override — the agent's own :class:`RetryPolicy` default applies.
    """
    value = cli if cli is not None else _env_int_optional("LOVIA_MAX_RETRIES")
    if value is not None and value < 0:
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
    # Long-lived server: curation must not hold back each run's final event.
    return Memory(root, curate_in_background=True)


def resolve_tools() -> list[Tool]:
    """The always-on built-in tools for the default agent.

    ``now`` (current time) and ``http_fetch`` have no extra dependencies. Web
    search prefers the Tavily backend when ``TAVILY_API_KEY`` is set; otherwise
    it falls back to the keyless ``ddgs`` backend (bundled with the
    ``web``/``ddg`` extras). When neither is available we load the rest and log
    how to enable it rather than failing.
    """
    tools: list[Tool] = [now, http_fetch]
    if os.environ.get("TAVILY_API_KEY"):
        tools.append(tavily_search())
    else:
        try:
            tools.append(duckduckgo_search())
        except UserError:
            log.info(
                "web_search disabled: set TAVILY_API_KEY or install the "
                "'ddgs' backend (pip install 'lovia[ddg]')."
            )
    return tools


def _env_bool(name: str) -> bool | None:
    """Parse a boolean-ish env var: True/False for set values, None when unset.

    Warns on a value that is neither truthy nor falsy — the likely mistake is
    putting a model spec in a flag (``LOVIA_VISION=openai:...`` instead of
    ``LOVIA_VISION_MODEL=openai:...``), which would otherwise silently read
    false.
    """
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    log.warning("%s=%r is not a boolean (use 1 or 0); treating as false", name, raw)
    return False


def resolve_vision_tool(
    provider: Provider, workspace: LocalWorkspace | None
) -> Tool | None:
    """The ``see_image`` tool, when a distinct vision model is configured.

    Wired only when all three hold: ``LOVIA_VISION_MODEL`` is set; the agent
    has a local workspace to read images from; and the *main* model can't see
    images itself. A vision-capable main model gets images inline, so a
    delegation tool would just be a redundant, slower second path — we log and
    skip it. Same env-gated shape as ``web_search`` in :func:`resolve_tools`.

    The vision model's endpoint and key default to the vendor env the spec
    routes to (``OPENAI_*`` / ``ANTHROPIC_*``); ``LOVIA_VISION_BASE_URL`` and
    ``LOVIA_VISION_API_KEY`` override them for a vision model that lives on a
    different endpoint than the main model (the common case).
    """
    spec = os.getenv("LOVIA_VISION_MODEL")
    if not spec:
        return None
    if workspace is None:
        log.info("LOVIA_VISION_MODEL set but no workspace; see_image disabled.")
        return None
    if supports_vision(provider):
        log.info(
            "main model is vision-capable; ignoring LOVIA_VISION_MODEL "
            "(images go inline as ImagePart)."
        )
        return None
    try:
        vision_provider = provider_from_string(
            spec,
            api_key=os.getenv("LOVIA_VISION_API_KEY"),
            base_url=os.getenv("LOVIA_VISION_BASE_URL"),
        )
    except UserError as exc:
        log.warning("LOVIA_VISION_MODEL=%r unusable; see_image disabled: %s", spec, exc)
        return None
    log.info("see_image enabled via vision model %r", spec)
    return make_see_image_tool(vision_provider, workspace_root=workspace.root)


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


def _mode_flag(args: argparse.Namespace) -> str | None:
    """Workspace mode selected by the boolean flags, if any."""
    return "readonly" if args.readonly else "trusted" if args.trusted else None


def resolve_workspace(
    cli_dir: str | None, cli_mode: str | None, no_workspace: bool
) -> LocalWorkspace | None:
    if no_workspace:
        return None
    root = _first(cli_dir, os.getenv("LOVIA_WORKSPACE")) or "."
    mode = _first(cli_mode, os.getenv("LOVIA_WORKSPACE_MODE")) or DEFAULT_WORKSPACE_MODE
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


def build_default_agent(
    args: argparse.Namespace, store: ChatStore, provider: Provider
) -> Agent[Any]:
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
    workspace = resolve_workspace(args.workspace, _mode_flag(args), args.no_workspace)
    tools = resolve_tools()
    vision_tool = resolve_vision_tool(provider, workspace)
    if vision_tool is not None:
        tools.append(vision_tool)
    agent: Agent[Any] = Agent(
        name=DEFAULT_AGENT_NAME,
        instructions=instructions,
        model=provider,
        settings=ModelSettings(max_tokens=resolve_max_tokens(args.max_tokens)),
        plugins=plugins,
        tools=tools,
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
        ("--base-url", args.base_url is not None),
        ("--api-key", args.api_key is not None),
        ("--skills-dir", bool(args.skills_dir)),
        ("--memory-dir", args.memory_dir is not None),
        ("--no-memory", args.no_memory),
        ("--workspace", args.workspace is not None),
        ("--readonly", args.readonly),
        ("--trusted", args.trusted),
        ("--no-workspace", args.no_workspace),
        ("--instructions", args.instructions is not None),
        ("--instructions-file", args.instructions_file is not None),
        ("--max-tokens", args.max_tokens is not None),
        ("--context-window", args.context_window is not None),
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
        env_sources = load_env_files(args.env_file)
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
            custom_agents = (
                agent_or_agents.values()
                if isinstance(agent_or_agents, Mapping)
                else [agent_or_agents]
            )
            for custom_agent in custom_agents:
                _warn_if_exposed(host, custom_agent.workspace)
            summary = setup.format_app_summary(
                version=__version__,
                app_target=app_target,
                # Without --db, create_app derives the file from the agent name.
                db_desc=db_path or "(./.lovia/<agent>.db, from the agent's name)",
                host=host,
                port=port,
            )
        else:
            db_desc = db_path or str(_default_db_path(DEFAULT_AGENT_NAME))
            conn = setup.resolve_connection(
                model_flag=args.model,
                base_url_flag=args.base_url,
                api_key_flag=args.api_key,
                context_window_flag=args.context_window,
                env_sources=env_sources,
            )
            if conn.missing():
                if not sys.stdin.isatty():
                    raise CliError(
                        f"no {' or '.join(conn.missing())} configured",
                        hint=setup.CONFIG_HINT,
                    )
                conn = setup.interactive_setup(conn, env_sources=env_sources)
            assert conn.model is not None
            try:
                provider = provider_from_string(
                    conn.model,
                    api_key=conn.api_key,
                    base_url=conn.base_url,
                    supports_vision=_env_bool("LOVIA_VISION"),
                )
            except ValueError as exc:
                # Eager construction also surfaces vendor-prefix typos at
                # startup instead of on the first chat message.
                raise CliError(str(exc)) from exc
            # The store is created only after setup succeeds so an aborted
            # first-run wizard leaves no stray database behind.
            store = ChatStore.sqlite(db_desc)
            agent = build_default_agent(args, store, provider)
            context_policy = Compaction(context_window=conn.context_window)
            _warn_if_exposed(host, agent.workspace)
            summary = setup.format_summary(
                conn,
                version=__version__,
                host=host,
                port=port,
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
