"""Assembly of the CLI's default agent: tools, plugins, workspace, models.

Everything arrives resolved: a provider, the Settings-managed
:class:`~lovia.web.config.WebConfig`, and the :class:`Deployment` — the few
launch flags that constrain what the browser may change. Shared by the CLI
boot and runtime reconfiguration, so both build agents through one path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args

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
    now,
    tavily_search,
    web_fetch,
)
from ..workspace import DEV_TOOLS, READ_ONLY, LocalWorkspace, Workspace, WorkspaceMode
from ..workspace.policy import CommandRule
from .charts import show_chart
from .config import (
    ModelProfile,
    RunSettings,
    SearchConfig,
    WebConfig,
    resolve_skills_dirs,
)
from .html import show_html
from .scheduling import Scheduling
from .store import ChatStore
from .tables import show_table
from .ui import SURFACE_NOTE
from .vision import make_describe_image_tool

log = logging.getLogger("lovia.web.builder")

WORKSPACE_MODES: tuple[str, ...] = get_args(WorkspaceMode)
INSTRUCTIONS_FILES: tuple[str, ...] = ("AGENTS.md",)
DEFAULT_AGENT_NAME = "lovia"
# The general workspace: where chats that belong to no project work (and
# where `lovia web` works when launched from the home or root directory, so
# its files never land loose in either). Created on first use.
GENERAL_WORKSPACE = Path("~/.lovia/workspace")
# The command bundles a ``coding`` workspace runs without asking, by name
# (Settings → Permissions; ``workspace.auto_allow`` in config.json). Core opts
# into none; `lovia web` is a coding agent the person running it launched, so
# inspection that prints no file contents and the project's own
# test/lint/build tools need no card — a prompt for `ls` or `pytest` is noise
# that trains people to click Approve. An empty list restores the core
# default. The baseline (dangerous commands, credential stores, protected
# config) applies either way.
AUTO_ALLOW_BUNDLES: dict[str, tuple[CommandRule, ...]] = {
    "read-only": READ_ONLY,
    "dev-tools": DEV_TOOLS,
}
GENERIC_INSTRUCTIONS = (
    "You are a helpful assistant running in the lovia web UI. "
    "Be concise and accurate, and use your tools and skills when they help."
)


@dataclass(frozen=True)
class Deployment:
    """The launch flags that bound what Settings can change.

    Everything else about the default agent lives in ``config.json``; these
    stay flags because they protect the deployment *from* the browser:

    * ``workspace`` — the root (``None`` for ``--no-workspace``).
    * ``readonly`` — ``--readonly``: every run is read-only, whatever the
      configured default mode says.
    * ``loopback`` — the server binds a loopback address. Off it, the
      browser (anyone holding the token) must not widen permissions, and
      ``trusted`` is never offered.
    * ``allow_workspace_edit`` — ``--allow-workspace-switch``: a non-loopback
      deployment opts into editing the workspace settings, and opening other
      directories, from the browser anyway (``trusted`` stays loopback-only).
    * ``lock_workspace`` — ``--lock-workspace``: every chat works in
      ``workspace``; it wins over ``allow_workspace_edit``.
    """

    workspace: str | None = "."
    readonly: bool = False
    loopback: bool = True
    allow_workspace_edit: bool = False
    lock_workspace: bool = False

    def roots(self) -> Literal["any"] | None:
        """Which directories a chat may open: ``"any"`` (plus the general
        workspace), or ``None`` — only ``workspace``. Off loopback, unless
        ``--allow-workspace-switch``, a token holder must not reach beyond
        the directory the deployer launched in."""
        if self.workspace is None or self.lock_workspace:
            return None
        if not self.loopback and not self.allow_workspace_edit:
            return None
        return "any"

    def remotes(self) -> Literal["any"] | None:
        """Whether a chat may open a workspace on another machine — an SSH
        destination, a running container: only on loopback, and only where
        chats may pick a directory at all. Not with
        ``--allow-workspace-switch``: that opens this server's directories to
        a token holder, where this would lend them its keys to other hosts."""
        return "any" if self.loopback and self.roots() == "any" else None

    def modes(self) -> tuple[str, ...]:
        """The workspace modes this deployment allows, narrowest first."""
        if self.workspace is None:
            return ()
        if self.readonly:
            return ("readonly",)
        return WORKSPACE_MODES if self.loopback else ("readonly", "coding")

    def effective_mode(self, configured: str) -> str | None:
        """The mode a run gets for the configured default (``None``: no
        workspace). An unavailable mode caps to the widest allowed one —
        modes are ordered, so the allowed set is always a prefix."""
        allowed = self.modes()
        if not allowed:
            return None
        return configured if configured in allowed else allowed[-1]

    def chat_modes(self, default_mode: str) -> tuple[str, ...]:
        """The modes a chat may pick. Off loopback, unless
        ``--allow-workspace-switch``, nothing wider than the mode the server
        started with: a token holder must not widen what the deployer set."""
        allowed = self.modes()
        if self.loopback or self.allow_workspace_edit or not allowed:
            return allowed
        ceiling = self.effective_mode(default_mode)
        return allowed[: allowed.index(ceiling) + 1] if ceiling else allowed

    def workspace_lock(self) -> str | None:
        """Why the browser can't edit the workspace settings, or ``None``.

        ``"no_workspace"`` / ``"readonly"`` name the flag; ``"remote"`` is a
        non-loopback bind without ``--allow-workspace-switch``.
        """
        if self.workspace is None:
            return "no_workspace"
        if self.readonly:
            return "readonly"
        if not self.loopback and not self.allow_workspace_edit:
            return "remote"
        return None


def resolve_memory(enabled: bool, root: str) -> Memory | None:
    """The default :class:`Memory` plugin, rooted at ``root``, unless disabled.

    The directory need not exist yet — the notes file and archive db are
    created under it on first write.
    """
    if not enabled:
        return None
    path = Path(root).expanduser()
    if path.exists() and not path.is_dir():
        raise UserError(f"memory path is not a directory: {path}")
    log.info("memory enabled at %s", path)
    # Long-lived server: curation must not hold back each run's final event.
    return Memory(str(path), curate_in_background=True)


def resolve_tools(search: SearchConfig | None = None) -> list[Tool]:
    """The always-on built-in tools for the default agent.

    ``now`` and ``web_fetch`` have no extra dependencies.
    Web search follows the configured backend: ``auto`` uses Tavily when a
    key is configured and the keyless ``ddgs`` backend otherwise. When the
    chosen backend can't run we load the rest and log how to enable it
    rather than failing.
    """
    tools: list[Tool] = [now, web_fetch()]
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
            extra_body=profile.extra_body,
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


def resolve_aux_model(profile: ModelProfile | None) -> Provider | None:
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
            extra_body=profile.extra_body,
        )
    except (UserError, ValueError) as exc:
        log.warning(
            "aux model %r unusable; falling back to the agent's own model: %s",
            profile.model,
            exc,
        )
        return None


def resolve_retry(run: RunSettings) -> RetryPolicy | None:
    """The server's retry override for ``run.max_retries`` (``None``: the
    agent's own posture applies)."""
    if run.max_retries is None:
        return None
    return RetryPolicy(max_attempts=run.max_retries + 1)


def resolve_instructions(custom: str = "", root: str | Path = ".") -> str:
    """The default agent's system prompt for a chat working in ``root``.

    ``custom`` (Settings → Agent) is the server-wide prompt; the directory's
    ``AGENTS.md`` (the cross-agent convention) is appended to it. Without
    one the file alone serves, and without either a generic prompt. Read on
    every build — and, with chats in several directories, on every run — so
    an edited ``AGENTS.md`` applies without a restart.
    """
    project = ""
    for name in INSTRUCTIONS_FILES:
        path = Path(root) / name
        if path.is_file():
            log.debug("using instructions from %s", path)
            project = path.read_text(encoding="utf-8")
            break
    custom = custom.strip()
    if custom and project:
        return f"{custom}\n\n{project}"
    return custom or project or GENERIC_INSTRUCTIONS


def project_agent(agent: Agent[Any], root: Path, config: WebConfig) -> Agent[Any]:
    """``agent`` for a chat working in ``root`` — ``WorkspaceSwitch.agent_for``
    for the CLI: that directory's ``AGENTS.md`` and skills (the configured
    skill directories, relative ones resolved against ``root``) in place of
    the launch directory's. Read on every run; nothing is cached."""
    plugins: list[Plugin] = [p for p in agent.plugins if not isinstance(p, Skills)]
    dirs = resolve_skills_dirs(config.skills, base=root, quiet=True)
    if dirs:
        plugins.insert(0, Skills(*dirs))
    return agent.clone(
        instructions=resolve_instructions(config.agent.instructions, root),
        plugins=plugins,
    )


def remote_agent(agent: Agent[Any], config: WebConfig) -> Agent[Any]:
    """``agent`` for a chat on a remote host — ``WorkspaceSwitch.agent_for_remote``
    for the CLI. The served agent was built for the launch directory; a
    project on another machine gets neither that directory's ``AGENTS.md``
    nor its skills: just the server-wide prompt, and the skill directories
    that are not relative to a project (``~/.lovia/skills`` and the like)."""
    plugins: list[Plugin] = [p for p in agent.plugins if not isinstance(p, Skills)]
    anywhere = config.skills.model_copy(
        update={
            "dirs": [
                d for d in config.skills.dirs if Path(d).expanduser().is_absolute()
            ]
        }
    )
    dirs = resolve_skills_dirs(anywhere, quiet=True)
    if dirs:
        plugins.insert(0, Skills(*dirs))
    return agent.clone(
        instructions=config.agent.instructions.strip() or GENERIC_INSTRUCTIONS,
        plugins=plugins,
    )


def resolve_workspace(
    deployment: Deployment, config: WebConfig | None = None
) -> LocalWorkspace | None:
    """The default agent's workspace: the deployment's root, in the
    configured mode as the deployment allows it."""
    settings = (config or WebConfig()).workspace
    mode = deployment.effective_mode(settings.default_mode)
    if mode is None or deployment.workspace is None:
        return None
    path = Path(deployment.workspace)
    if not path.is_dir():
        raise UserError(f"workspace directory not found: {path}")
    if mode != "coding":
        return Workspace.local(str(path), mode=cast(WorkspaceMode, mode))
    rules = tuple(
        rule for name in settings.auto_allow for rule in AUTO_ALLOW_BUNDLES[name]
    )
    return Workspace.local(str(path), mode="coding", command_rules=rules)


def build_default_agent(
    deployment: Deployment,
    store: ChatStore,
    provider: Provider,
    question_channel: HumanChannel | None = None,
    *,
    config: WebConfig | None = None,
) -> Agent[Any]:
    """The ready-made agent ``lovia web`` serves.

    ``config`` supplies everything Settings manages (search backend, vision
    role, skill directories, workspace mode, memory, subagents, the prompt);
    ``deployment`` bounds the workspace.
    """
    config = config or WebConfig()
    profile = config.default_profile()
    settings = ModelSettings(max_tokens=profile.max_tokens if profile else None)
    root = Path(deployment.workspace or ".")
    instructions = resolve_instructions(config.agent.instructions, root)
    skills_dirs = resolve_skills_dirs(config.skills, base=root)
    plugins: list[Plugin] = []
    if skills_dirs:
        plugins.append(Skills(*skills_dirs))
        log.info("loaded skills from %s", ", ".join(str(d) for d in skills_dirs))
    plugins.append(Todo())
    # Let the model create Scheduled runs from chat (gated by approval). Closes
    # over the app's store so it writes the rows the scheduler polls.
    plugins.append(Scheduling(store))
    memory = resolve_memory(config.agent.memory, config.agent.memory_dir)
    if memory is not None:
        plugins.append(memory)
    workspace = resolve_workspace(deployment, config)
    tools = resolve_tools(config.search)
    vision_tool = resolve_vision_tool(provider, workspace, config.vision_profile())
    if vision_tool is not None:
        tools.append(vision_tool)
    if config.agent.subagents:
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
            settings=settings,
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
    # Parent-only too: a background child's transcript has no one watching.
    tools = [*tools, show_chart, show_table, show_html]
    agent: Agent[Any] = Agent(
        name=DEFAULT_AGENT_NAME,
        instructions=instructions,
        model=provider,
        settings=settings,
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
