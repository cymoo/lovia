"""Assembly of the CLI's default agent: tools, plugins, workspace, models.

Everything model-flavoured arrives resolved (a provider, a
:class:`~lovia.web.config.WebConfig`) — this module reads the environment
only for the agent-topology knobs that stay flag/env-driven (workspace,
memory, skills, turn/token caps). Shared by the CLI boot and, from 0.9.38,
runtime reconfiguration — both build agents through the same path.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any, cast, get_args

from ..agent import Agent
from ..exceptions import UserError
from ..plugins import Memory, Plugin, Skills, Subagents, Todo
from ..providers import ModelSettings, Provider, provider_from_string, supports_vision
from ..reliability import RetryPolicy
from ..tools import (
    HumanChannel,
    Tool,
    ask_human,
    current_date,
    duckduckgo_search,
    http_request,
    now,
    read_page,
    tavily_search,
)
from ..workspace import LocalWorkspace, Workspace, WorkspaceMode
from .config import ModelProfile, SearchConfig, WebConfig
from .scheduling import Scheduling
from .store import ChatStore
from .ui import SURFACE_NOTE
from .vision import make_describe_image_tool

log = logging.getLogger("lovia.web.builder")

WORKSPACE_MODES: tuple[str, ...] = get_args(WorkspaceMode)
INSTRUCTIONS_FILES: tuple[str, ...] = ("AGENTS.md",)
DEFAULT_SKILLS_DIR = ".agents/skills"
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
        raise UserError(f"invalid integer for {name}: {raw!r}") from exc


def _env_int_optional(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise UserError(f"invalid integer for {name}: {raw!r}") from exc


def _env_bool(name: str) -> bool | None:
    """Parse a boolean-ish env var: True/False for set values, None when unset."""
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


def resolve_max_retries(cli: int | None) -> int | None:
    """Explicit provider retry count, or ``None`` for the agent's posture.

    Precedence: ``--max-retries`` flag, then ``LOVIA_MAX_RETRIES``. ``None``
    means no override — the agent's own :class:`RetryPolicy` default applies.
    """
    value = cli if cli is not None else _env_int_optional("LOVIA_MAX_RETRIES")
    if value is not None and value < 0:
        raise UserError(f"--max-retries must be >= 0, got {value}")
    return value


def resolve_max_turns(cli: int | None) -> int:
    """Per-run agent turn cap (flag > LOVIA_MAX_TURNS > 50)."""
    value = cli if cli is not None else _env_int("LOVIA_MAX_TURNS", DEFAULT_MAX_TURNS)
    if value < 1:
        raise UserError(f"--max-turns must be >= 1, got {value}")
    return value


def resolve_max_tokens(cli: int | None) -> int | None:
    """Max output tokens per response (flag > LOVIA_MAX_TOKENS > provider default)."""
    value = cli if cli is not None else _env_int_optional("LOVIA_MAX_TOKENS")
    if value is not None and value <= 0:
        raise UserError(f"--max-tokens must be > 0, got {value}")
    return value


def resolve_skills_dirs(cli_dirs: list[str] | None) -> list[Path]:
    """Skill directories: ``--skills-dir`` > ``LOVIA_SKILLS_DIR`` > the default.

    The default is ``./.agents/skills`` when present — the cross-agent
    convention (shared with Claude Code, Codex, OpenCode), pairing with the
    ``AGENTS.md`` instructions default. A bare ``./skills`` was the default
    before 0.9.13; it is no longer auto-loaded (the generic name collides
    with non-skill directories), so we point at the move once at startup.
    """
    if cli_dirs:
        dirs = [Path(d) for d in cli_dirs]
        for d in dirs:
            if not d.is_dir():
                raise UserError(f"skills directory not found: {d}")
        return dirs
    env = os.getenv("LOVIA_SKILLS_DIR")
    if env:
        d = Path(env)
        if not d.is_dir():
            raise UserError(f"skills directory not found (LOVIA_SKILLS_DIR): {d}")
        return [d]
    default = Path(DEFAULT_SKILLS_DIR)
    if default.is_dir():
        return [default]
    if Path("skills").is_dir():
        log.warning(
            "./skills is no longer auto-loaded; move it to ./%s "
            "or pass --skills-dir skills",
            DEFAULT_SKILLS_DIR,
        )
    return []


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
        raise UserError(f"memory path is not a directory: {path}")
    log.info("memory enabled at %s", path)
    # Long-lived server: curation must not hold back each run's final event.
    return Memory(root, curate_in_background=True)


def resolve_tools(search: SearchConfig | None = None) -> list[Tool]:
    """The always-on built-in tools for the default agent.

    ``now``, ``read_page`` and ``http_request`` have no extra dependencies.
    Web search follows the configured backend: ``auto`` uses Tavily when a
    key is configured and the keyless ``ddgs`` backend otherwise. When the
    chosen backend can't run we load the rest and log how to enable it
    rather than failing.
    """
    tools: list[Tool] = [now, read_page, http_request]
    search = search or SearchConfig()
    backend = search.backend
    if backend == "auto":
        backend = "tavily" if search.tavily_api_key else "ddg"
    if backend == "tavily":
        if search.tavily_api_key:
            tools.append(tavily_search(api_key=search.tavily_api_key))
        else:
            log.warning(
                "search backend 'tavily' has no API key; web_search disabled "
                "— set it in Settings (or config.json)."
            )
    elif backend == "ddg":
        try:
            tools.append(duckduckgo_search())
        except UserError:
            log.info(
                "web_search disabled: configure a Tavily API key or install "
                "the 'ddgs' backend (pip install 'lovia[ddg]')."
            )
    return tools


def resolve_vision_tool(
    provider: Provider,
    workspace: LocalWorkspace | None,
    profile: ModelProfile | None,
) -> Tool | None:
    """The ``describe_image`` tool, when a distinct vision model is configured.

    Wired only when all three hold: the config assigns a vision role; the
    agent has a local workspace to read images from; and the *main* model
    can't see images itself. A vision-capable main gets the core
    ``view_image`` tool from the workspace bundle instead (real pixels), so
    the two are mutually exclusive by construction — a delegation tool there
    would just be a redundant, slower second path.
    """
    if profile is None:
        return None
    if workspace is None:
        log.info(
            "a vision model is configured but there is no workspace; "
            "describe_image disabled."
        )
        return None
    if supports_vision(provider):
        log.info(
            "main model is vision-capable; ignoring the vision role "
            "(it gets view_image / inline ImageParts instead)."
        )
        return None
    try:
        vision_provider = provider_from_string(
            profile.spec(),
            api_key=profile.api_key,
            base_url=profile.base_url,
            supports_vision=profile.vision_override(),
        )
    except (UserError, ValueError) as exc:
        log.warning(
            "vision model %r unusable; describe_image disabled: %s",
            profile.model,
            exc,
        )
        return None
    log.info("describe_image enabled via vision model %r", profile.model)
    return make_describe_image_tool(vision_provider)


def resolve_followup_model(profile: ModelProfile | None) -> Provider | None:
    """The aux-role model for titles and follow-up suggestions, if assigned.

    ``None`` falls back to the agent's own model. An unusable profile must
    not take the whole server down — it logs and falls back instead.
    """
    if profile is None:
        return None
    try:
        return provider_from_string(
            profile.spec(),
            api_key=profile.api_key,
            base_url=profile.base_url,
        )
    except (UserError, ValueError) as exc:
        log.warning(
            "aux model %r unusable; falling back to the agent's own model: %s",
            profile.model,
            exc,
        )
        return None


def resolve_followups(cli_off: bool) -> bool:
    """Follow-up chips: on unless ``--no-followups`` or ``LOVIA_FOLLOWUPS=0``.

    The opposite of the library default (``create_app(followups=False)``), on
    purpose: an embedder must never silently gain a model call per run, while
    the bundled CLI is a finished chat app where the chips are part of it.
    """
    if cli_off:
        return False
    env = _env_bool("LOVIA_FOLLOWUPS")
    return True if env is None else env


def resolve_instructions(cli_text: str | None, cli_file: str | None) -> str:
    if cli_text is not None:
        return cli_text
    file = _first(cli_file, os.getenv("LOVIA_INSTRUCTIONS_FILE"))
    if file:
        path = Path(file)
        if not path.is_file():
            raise UserError(f"instructions file not found: {path}")
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
    mode = _first(cli_mode, os.getenv("LOVIA_WORKSPACE_MODE")) or DEFAULT_WORKSPACE_MODE
    if mode not in WORKSPACE_MODES:
        raise UserError(
            f"invalid workspace mode: {mode!r}",
            hint=f"choose one of: {', '.join(WORKSPACE_MODES)}",
        )
    path = Path(root)
    if not path.is_dir():
        raise UserError(f"workspace directory not found: {path}")
    return Workspace.local(str(path), mode=cast(WorkspaceMode, mode))


def build_default_agent(
    args: argparse.Namespace,
    store: ChatStore,
    provider: Provider,
    question_channel: HumanChannel | None = None,
    *,
    config: WebConfig | None = None,
) -> Agent[Any]:
    """The ready-made agent ``lovia web`` serves.

    ``config`` supplies the model-flavoured extras (search backend, vision
    role); the argparse namespace supplies the topology flags.
    """
    config = config or WebConfig()
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
    tools = resolve_tools(config.search)
    vision_tool = resolve_vision_tool(provider, workspace, config.vision_profile())
    if vision_tool is not None:
        tools.append(vision_tool)
    if not args.no_subagents:
        # Background subagents, on by default. The child is the same
        # assistant minus the plugins that must not ride along: Subagents
        # itself (no recursive spawning), Scheduling (a child schedules
        # nothing), and Memory (a task transcript is not user memory) —
        # Skills and Todo are basic capabilities a delegated task needs.
        # ask_human is likewise parent-only (added below): a delegated
        # background task has no operator watching to answer it.
        child: Agent[Any] = Agent(
            name=f"{DEFAULT_AGENT_NAME}-sub",
            instructions=instructions,
            model=provider,
            settings=ModelSettings(max_tokens=resolve_max_tokens(args.max_tokens)),
            plugins=[p for p in plugins if isinstance(p, (Skills, Todo))],
            tools=tools,
            workspace=workspace,
        )
        child.instruction(current_date())
        # Cap below max_background_runs (8): parent + children + scheduler
        # fires must all fit without starving each other.
        plugins.append(Subagents([child], max_concurrent=3))
    if question_channel is not None:
        tools = [*tools, ask_human(question_channel)]
    agent: Agent[Any] = Agent(
        name=DEFAULT_AGENT_NAME,
        instructions=instructions,
        model=provider,
        settings=ModelSettings(max_tokens=resolve_max_tokens(args.max_tokens)),
        plugins=plugins,
        tools=tools,
        workspace=workspace,
    )
    # This agent only ever runs in the bundled UI, so declare what it renders —
    # untold, models assume plain text and refuse to embed images. Static, so
    # it precedes the fragment that changes daily.
    agent.instruction(lambda _ctx: SURFACE_NOTE)
    # Tell the model today's date up front so it searches the current year and
    # skips the now->web_search round-trip. Date only (server-local tz): stable
    # within a prompt-cache window; precise time stays the `now` tool's job.
    agent.instruction(current_date())
    return agent


def _mode_flag(args: argparse.Namespace) -> str | None:
    """Workspace mode selected by the boolean flags, if any."""
    return "readonly" if args.readonly else "trusted" if args.trusted else None
