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

from ..agent import Agent, InstructionsFn
from ..exceptions import UserError
from ..plugins import Memory, Plugin, Skills, Subagents, Todo
from ..providers import ModelSettings, Provider, provider_from_string, supports_vision
from ..reliability import RetryPolicy
from ..run_context import RunContext
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
from ..workspace import (
    DEV_TOOLS,
    READ_ONLY,
    LocalWorkspace,
    Workspace,
    WorkspaceError,
    WorkspaceMode,
)
from ..workspace.local import LocalWorkspaceSession, instruction_files
from ..workspace.policy import CommandRule, WorkspacePolicy
from ..workspace.protocol import WorkspaceSession
from ..workspace.remote.session import RemoteWorkspaceSession
from ..workspace.types import InstructionFile
from .charts import show_chart
from ..context import Compaction
from .config import (
    DEFAULT_AGENT_ID,
    DEFAULT_HOME,
    AgentConfig,
    ModelProfile,
    SearchConfig,
    WebConfig,
    config_path,
    resolve_skills_dirs,
)
from .html import show_html
from .mcp import MCPBook, SettingsMCP
from .run_deps import chat_project
from .scheduling import Scheduling
from .store import ChatStore
from .tables import show_table
from .ui import SURFACE_NOTE
from .vision import make_describe_image_tool

log = logging.getLogger("lovia.web.builder")

WORKSPACE_MODES: tuple[str, ...] = get_args(WorkspaceMode)
# A configured agent's background child is ``<id>.sub``: ids have no dots,
# so the part before one always names the agent whose settings apply.
SUBAGENT_SUFFIX = ".sub"
# The command bundles a ``coding`` workspace runs without asking, by name
# (Settings → Agents; ``agents[].auto_allow`` in config.json). Core opts
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
    stay flags because they protect the deployment *from* the browser. They
    are the only bounds: the address the server binds changes nothing (it
    says nothing about who sits at the browser — a container must bind
    ``0.0.0.0`` for its own deployer), so tightening is always explicit.

    * ``workspace`` — the root (``None`` for ``--no-workspace``).
    * ``readonly`` — ``--readonly``: every run is read-only, whatever the
      configured default mode says.
    * ``lock_workspace`` — ``--lock-workspace``: every chat works in
      ``workspace``, on this machine.
    * ``home`` — ``--home``: lovia's home, holding memory and the general
      workspace (and ``config.json`` and the chat history, which the CLI
      opens itself).
    """

    workspace: str | None = "."
    readonly: bool = False
    lock_workspace: bool = False
    home: Path = DEFAULT_HOME

    def general_workspace(self) -> Path:
        """Where chats that belong to no project work (and where ``lovia
        web`` works when launched from the home or root directory, so its
        files never land loose in either). Created on first use."""
        return self.home.expanduser() / "workspace"

    def roots(self) -> Literal["any"] | None:
        """Which directories a chat may open: ``"any"`` (plus the general
        workspace), or ``None`` — only ``workspace``."""
        if self.workspace is None or self.lock_workspace:
            return None
        return "any"

    def remotes(self) -> Literal["any"] | None:
        """Whether a chat may open a workspace on another machine — an SSH
        destination, a running container: wherever chats may pick a
        directory at all. It adds no reach: an agent that can run a shell
        here can already run ``ssh``."""
        return "any" if self.roots() == "any" else None

    def modes(self) -> tuple[str, ...]:
        """The workspace modes this deployment allows, narrowest first."""
        if self.workspace is None:
            return ()
        if self.readonly:
            return ("readonly",)
        return WORKSPACE_MODES

    def effective_mode(self, configured: str) -> str | None:
        """The mode a run gets for the configured default (``None``: no
        workspace). An unavailable mode caps to the widest allowed one —
        modes are ordered, so the allowed set is always a prefix."""
        allowed = self.modes()
        if not allowed:
            return None
        return configured if configured in allowed else allowed[-1]

    def workspace_lock(self) -> str | None:
        """Why the browser can't edit the workspace settings, or ``None``:
        ``"no_workspace"`` / ``"readonly"``, naming the flag."""
        if self.workspace is None:
            return "no_workspace"
        if self.readonly:
            return "readonly"
        return None


def resolve_memory(enabled: bool, root: str | Path) -> Memory | None:
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
    # Split by project: notes about the user everywhere, the rest per chat
    # directory (the Memory page's two scopes).
    return Memory(
        str(path),
        curate_in_background=True,
        scope=chat_project,
        scope_name="project",
    )


def resolve_tools(
    search: SearchConfig | None = None, *, web_search: bool = True
) -> list[Tool]:
    """The built-in tools every configured agent gets.

    ``now`` and ``web_fetch`` have no extra dependencies.
    Web search, unless the agent turned it off (``web_search=False``),
    follows the configured backend: ``auto`` uses Tavily when a key is
    configured and the keyless ``ddgs`` backend otherwise. When the chosen
    backend can't run we load the rest and log how to enable it rather
    than failing.
    """
    tools: list[Tool] = [now, web_fetch()]
    if not web_search:
        return tools
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


def resolve_retry(max_retries: int | None) -> RetryPolicy:
    """An agent's retry policy: ``max_retries`` attempts after the first,
    or the core default for ``None``."""
    if max_retries is None:
        return RetryPolicy()
    return RetryPolicy(max_attempts=max_retries + 1)


def resolve_instructions(
    custom: str = "",
    root: str | Path = ".",
    policy: WorkspacePolicy | None = None,
) -> str:
    """An agent's system prompt for a chat working in ``root``.

    ``custom`` (the agent's own, from Settings → Agents) comes first; the
    project's ``AGENTS.md`` files (the cross-agent convention) follow it,
    from the Git root down to ``root`` (:func:`instruction_files`, whose
    explicit rules come from ``policy``; default: the baseline). Without
    one the files alone serve, and without either a generic prompt. Read on
    every build — and, with chats in several directories, on every run — so
    an edited ``AGENTS.md`` applies without a restart.
    """
    files = instruction_files(Path(root), policy or WorkspacePolicy())
    return _compose(custom, _project_text(files))


def instructions_policy(deployment: Deployment) -> WorkspacePolicy:
    """The rules a project's ``AGENTS.md`` loads under on this machine: what
    every chat's workspace holds whatever its mode (:func:`resolve_workspace`)."""
    return WorkspacePolicy(denied_paths=_home_secrets(deployment))


def _project_text(files: list[InstructionFile]) -> str:
    """``files`` as one prompt section. The root's own file alone reads as
    it always has; anything more says where each part comes from."""
    for f in files:
        if f.skipped:
            log.warning("not using %s: %s", f.path, f.skipped)
    used = [f for f in files if not f.skipped]
    if not used:
        return ""
    log.info("instructions from %s", ", ".join(f.path for f in used))
    if len(used) == 1 and used[0].level == 0:
        return used[0].text
    return "\n\n".join(f"Instructions from {f.path}:\n{f.text.strip()}" for f in used)


def _compose(custom: str, project: str) -> str:
    custom = custom.strip()
    if custom and project:
        return f"{custom}\n\n{project}"
    return custom or project or GENERIC_INSTRUCTIONS


def remote_instructions(custom: str = "") -> InstructionsFn:
    """:func:`resolve_instructions` for a chat on a remote host: the same
    composition, with the project's ``AGENTS.md`` files found and read where
    the project is, under the chat's own policy's rules.

    Once per run — the caller builds one of these per run. Every later
    render in that run returns the same text, which keeps the prompt prefix
    stable. Instructions that cannot be read are treated as absent: the run
    then says what is wrong with the host far better than a prompt could.
    """
    rendered: str | None = None

    async def instructions(ctx: RunContext[Any]) -> str:
        nonlocal rendered
        if rendered is None:
            rendered = _compose(custom, await _remote_project_text(ctx.workspace))
        return rendered

    return instructions


async def _remote_project_text(session: WorkspaceSession | None) -> str:
    if not isinstance(session, (LocalWorkspaceSession, RemoteWorkspaceSession)):
        return ""
    try:
        files = await session.read_instructions()
    except (OSError, UserError, WorkspaceError) as exc:
        log.warning("not using the remote project's instructions: %s", exc)
        return ""
    return _project_text(files)


def spec_id(name: str) -> str:
    """The id of the configured agent whose settings the agent named
    ``name`` runs on — its own, or the one a ``<id>.sub`` child belongs to."""
    return name.partition(".")[0]


def project_agent(
    agent: Agent[Any],
    root: Path,
    config: WebConfig,
    spec: AgentConfig,
    policy: WorkspacePolicy | None = None,
) -> Agent[Any]:
    """``agent`` for a chat working in ``root`` — ``WorkspaceSwitch.agent_for``
    for the CLI: that project's ``AGENTS.md`` files (read under ``policy``'s
    rules, :func:`resolve_instructions`) and the directory's ``.agents/skills``
    (ahead of ``spec``'s skill directories) in place of the launch
    directory's. Read on every run; nothing is cached."""
    plugins: list[Plugin] = [p for p in agent.plugins if not isinstance(p, Skills)]
    dirs = resolve_skills_dirs(config.agent_skills(spec), root, quiet=True)
    if dirs:
        plugins.insert(0, Skills(*dirs))
    return agent.clone(
        instructions=resolve_instructions(spec.instructions, root, policy),
        plugins=plugins,
    )


def remote_agent(agent: Agent[Any], config: WebConfig, spec: AgentConfig) -> Agent[Any]:
    """``agent`` for a chat on a remote host — ``WorkspaceSwitch.agent_for_remote``
    for the CLI. The served agent was built for the launch directory; a
    project on another machine gets neither that directory's ``AGENTS.md``
    nor its skills. It gets ``spec``'s prompt with its own ``AGENTS.md`` files
    (:func:`remote_instructions`), and ``spec``'s global skill directories —
    the skills plugin reads this machine's disk, so the project's own stay
    unloaded (#405)."""
    plugins: list[Plugin] = [p for p in agent.plugins if not isinstance(p, Skills)]
    dirs = resolve_skills_dirs(config.agent_skills(spec), quiet=True)
    if dirs:
        plugins.insert(0, Skills(*dirs))
    return agent.clone(
        instructions=remote_instructions(spec.instructions),
        plugins=plugins,
    )


def resolve_workspace(
    deployment: Deployment,
    spec: AgentConfig | None = None,
    *,
    root: str | Path | None = None,
    mode: str | None = None,
) -> LocalWorkspace | None:
    """``spec``'s workspace: in ``root`` (default: the deployment's) and
    ``mode`` (default: ``spec``'s), as the deployment allows it, with
    ``spec``'s auto-allowed commands when that mode is ``coding``."""
    spec = spec or AgentConfig(id=DEFAULT_AGENT_ID)
    mode = deployment.effective_mode(mode or spec.mode)
    if mode is None or deployment.workspace is None:
        return None
    path = Path(root if root is not None else deployment.workspace)
    if not path.is_dir():
        raise UserError(f"workspace directory not found: {path}")
    denied = _home_secrets(deployment)
    if mode != "coding":
        return Workspace.local(
            str(path), mode=cast(WorkspaceMode, mode), denied_paths=denied
        )
    rules = tuple(rule for name in spec.auto_allow for rule in AUTO_ALLOW_BUNDLES[name])
    return Workspace.local(
        str(path), mode="coding", command_rules=rules, denied_paths=denied
    )


def _home_secrets(deployment: Deployment) -> tuple[str, ...]:
    """``config.json`` holds the model API keys, so no agent tool may open
    it. The workspace baseline names it in the default home; a ``--home``
    elsewhere — inside the workspace, even — is named here."""
    if deployment.home.expanduser() == DEFAULT_HOME.expanduser():
        return ()
    return (config_path(deployment.home).resolve().with_suffix(".*").as_posix(),)


def build_agent(
    deployment: Deployment,
    store: ChatStore,
    provider: Provider,
    question_channel: HumanChannel | None = None,
    *,
    config: WebConfig | None = None,
    spec: AgentConfig | None = None,
    memory: Memory | None = None,
    mcp: MCPBook | None = None,
) -> Agent[Any]:
    """One agent ``lovia web`` serves, from ``spec`` (default: the
    configuration's default agent) on ``provider``.

    ``config`` supplies what is global (search backend, vision role, the
    skill directories ``spec`` inherits); ``deployment`` bounds the
    workspace and names the home memory lives in. ``memory`` is the plugin
    to share when ``spec`` keeps memory — every agent of one configuration
    must share one, it owns the notes file's lock — and ``None`` builds one.
    ``mcp`` records how each MCP server's connections go and where a run
    reports one it started without (``ConfigRuntime`` passes its own); the
    enabled servers of ``config.mcp`` load when ``spec.mcp`` is on.
    The core ``Agent.name`` is ``spec.id``: what chats store.
    """
    config = config or WebConfig()
    spec = spec or config.default_agent
    profile = config.agent_profile(spec)
    settings = ModelSettings(max_tokens=profile.max_tokens if profile else None)
    # Compaction sizes itself to the window the profile states, else the
    # provider's advertised one; per agent, since each may run another model.
    policy = Compaction(context_window=profile.context_window if profile else None)
    retry = resolve_retry(spec.max_retries)
    root = Path(deployment.workspace or ".")
    instructions = resolve_instructions(
        spec.instructions, root, instructions_policy(deployment)
    )
    skills_dirs = resolve_skills_dirs(config.agent_skills(spec), root)
    plugins: list[Plugin] = []
    if skills_dirs:
        plugins.append(Skills(*skills_dirs))
        log.info("loaded skills from %s", ", ".join(str(d) for d in skills_dirs))
    plugins.append(Todo())
    # Let the model create Scheduled runs from chat (gated by approval). Closes
    # over the app's store so it writes the rows the scheduler polls.
    plugins.append(Scheduling(store))
    if spec.memory:
        if memory is None:
            memory = resolve_memory(True, deployment.home / "memory")
        plugins.append(cast(Memory, memory))
    servers = [s for s in config.mcp if s.enabled]
    if spec.mcp and servers:
        plugins.append(SettingsMCP(servers, mcp if mcp is not None else MCPBook()))
    workspace = resolve_workspace(deployment, spec)
    tools = resolve_tools(config.search, web_search=spec.search)
    vision_tool = resolve_vision_tool(provider, workspace, config.vision_profile())
    if vision_tool is not None:
        tools.append(vision_tool)
    if spec.subagents:
        # Background subagents, on by default. The child is the same
        # assistant minus the plugins that must not ride along: Subagents
        # itself (no recursive spawning), Scheduling (a child schedules
        # nothing), and Memory (a task transcript is not user memory) —
        # Skills and Todo are basic capabilities a delegated task needs.
        # ask_human is likewise parent-only (added below): a delegated
        # background task has no operator watching to answer it.
        child: Agent[Any] = Agent(
            name=f"{spec.id}{SUBAGENT_SUFFIX}",
            instructions=instructions,
            model=provider,
            settings=settings,
            retry=retry,
            context_policy=policy,
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
        name=spec.id,
        instructions=instructions,
        model=provider,
        settings=settings,
        retry=retry,
        context_policy=policy,
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
