"""Runtime reconfiguration: build, serve and hot-swap the configured agents.

The single choke point for "the configuration changed": every run entry
(chat, stream, reconnect, scheduler fire, subagent task) resolves its Agent
through ``RouterDeps.agents`` at message time, and in-flight runs hold
``replace()`` copies — so atomically replacing the registry applies a new
configuration to every *next* message while running turns finish undisturbed
on the old providers.

Owned by :func:`~lovia.web.default_app` (what ``lovia web`` serves): an app
from ``create_app`` alone configures its agents in code and never sees this
class.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...agent import Agent
from ...exceptions import UserError
from ...plugins import Memory
from ...providers import Provider, provider_from_string
from ...providers._windows import clear_endpoint_cache
from ...run_context import RunContext
from ...tools import HumanChannel
from ...workspace import LocalWorkspace
from ..followups import FollowupRequest, generate_followups
from ..sse import ConfigChangedData
from .check import mask_key
from .schema import DEFAULT_AGENT_ID, Connection, WebConfig
from .storage import LoadedConfig, config_writable, save_config

if TYPE_CHECKING:
    from ..api.deps import RouterDeps
    from ..builder import Customize, Deployment
    from ..store import ChatStore

log = logging.getLogger("lovia.web.config")

# How long a retired provider may wait for its last run to finish before we
# give up and leak it (never close a client under a marathon run).
_RETIRE_TIMEOUT = 600.0
_RETIRE_POLL = 5.0


class ConfigRuntime:
    """Applies a :class:`WebConfig` to a running server.

    Built by the CLI with everything agent assembly needs (the deployment's
    launch constraints, the shared store, the ask_human channel), then bound
    to the app's ``RouterDeps`` by ``create_app``. ``apply()`` is the one
    write path: validate by building first, persist, then swap.

    It serves one agent, named :data:`DEFAULT_AGENT_ID`, with the
    ``assistant`` section's settings.
    """

    def __init__(
        self,
        *,
        deployment: "Deployment | None" = None,
        loaded: LoadedConfig,
        store: "ChatStore",
        question_channel: HumanChannel | None = None,
        customize: "Customize | None" = None,
    ) -> None:
        from ..builder import Deployment

        self.deployment = deployment if deployment is not None else Deployment()
        self.loaded = loaded
        self.store = store
        self.question_channel = question_channel
        # ``default_app(customize=)``: applied to every agent a build makes.
        self.customize = customize
        # Set by create_app when the app runs without its own auth guard —
        # the config write endpoints then insist on a loopback Host header
        # (the DNS-rebinding guard; see api/config.py).
        self.require_local_host = False
        self._deps: RouterDeps | None = None
        self._lock = asyncio.Lock()
        self._retire_tasks: set[asyncio.Task[None]] = set()
        # Built on first need and kept across ``apply``: it holds the notes
        # files' locks, so a run still on the old agents and the editor (or
        # clear_scope) on the new ones must share the one instance.
        self._memory: Memory | None = None
        # Each MCP server's last connection (Settings shows it), and where a
        # run reports one it started without: kept across ``apply`` too.
        from ..mcp import MCPBook
        from ..project_mcp import ProjectMCP

        self.mcp = MCPBook(unavailable=self._mcp_unavailable)
        # Which of a project's own servers (its ``.mcp.json``) are on.
        self.project_mcp = ProjectMCP(self.deployment.home)

    # ------------------------------------------------------------- state -

    @property
    def config(self) -> WebConfig:
        return self.loaded.config

    @property
    def writable(self) -> bool:
        """Whether Settings can save: a read-only ``config.json`` (say, a
        read-only mount) makes every config write refuse up front."""
        return config_writable(self.loaded.path)

    def bind(self, deps: "RouterDeps") -> None:
        """Attach the mutation surface (called once by ``create_app``)."""
        self._deps = deps

    @staticmethod
    def serveable(config: WebConfig) -> list[str]:
        """What stops ``config`` from serving; empty when it can.

        The completeness rule: a default profile must exist,
        and an official-host endpoint must have its key.
        """
        profile = config.default_profile()
        if profile is None:
            return ["model"]
        return Connection.from_profile(profile).missing()

    @property
    def configured(self) -> bool:
        return not self.serveable(self.config)

    def _mcp_unavailable(
        self, ctx: RunContext[Any], failed: list[tuple[str, str]]
    ) -> None:
        """Tell the chat whose run started without some MCP servers."""
        from ..sse import MCPUnavailable

        deps = self._deps
        ctrl = deps.supervisor.get(ctx.session_id) if deps and ctx.session_id else None
        if ctrl is not None:
            ctrl.announce(MCPUnavailable(servers=failed))

    # ---------------------------------------------------------- building -

    def build(
        self, config: WebConfig | None = None
    ) -> tuple[dict[str, Agent[Any]], Provider | None]:
        """Construct the served pieces for ``config`` (no side effects but
        the one Memory plugin, built on first need and reused by every build).

        Returns ``(agents, aux_model)``: the one served agent by name, and
        the aux model that serves titles and follow-ups, or ``None`` for
        "the agent's own". The CLI boot and ``apply`` both build through
        here, so a field can't reach one path and miss the other. Raises
        :class:`UserError` when the configuration cannot serve and
        ``ValueError`` for an unknown vendor prefix — callers turn both
        into their surface's error shape.
        """
        from ..builder import build_agent, resolve_aux_model, resolve_memory

        config = config if config is not None else self.config
        missing = self.serveable(config)
        if missing:
            raise UserError(f"no {' or '.join(missing)} configured")
        profile = config.default_profile()
        assert profile is not None  # serveable() checked it
        conn = Connection.from_profile(profile)
        assert conn.model is not None
        provider = provider_from_string(
            conn.model,
            api_key=conn.api_key,
            base_url=conn.base_url,
            supports_vision=profile.vision_override(),
            extra_body=profile.extra_body,
        )
        memory = None
        if config.assistant.memory:
            if self._memory is None:
                self._memory = resolve_memory(True, self.deployment.home / "memory")
            memory = self._memory
        agent = build_agent(
            self.deployment,
            self.store,
            provider,
            question_channel=self.question_channel,
            config=config,
            memory=memory,
            mcp=self.mcp,
            project_mcp=self.project_mcp,
            customize=self.customize,
        )
        return {agent.name: agent}, resolve_aux_model(config.aux_profile())

    def max_turns(self) -> dict[str, int]:
        """The turn limit — ``create_app(max_turns=)`` — for the served
        agent and its background child, which runs under its own name."""
        from ..builder import SUBAGENT_SUFFIX

        limit = self.config.assistant.max_turns
        return {DEFAULT_AGENT_ID: limit, f"{DEFAULT_AGENT_ID}{SUBAGENT_SUFFIX}": limit}

    def workspace_for(self, agent: Agent[Any], root: Path, mode: str) -> LocalWorkspace:
        """A chat's workspace in ``mode`` — ``WorkspaceSwitch.build`` for the
        CLI, with the auto-allowed commands. Built from the current
        configuration each run, so a change to them reaches every coding
        chat's next run."""
        from ..builder import resolve_workspace

        workspace = resolve_workspace(
            self.deployment, self.config.assistant, root=root, mode=mode
        )
        assert workspace is not None  # a switch exists only with a workspace
        return workspace

    def agent_for(self, agent: Agent[Any], root: Path) -> Agent[Any]:
        """``WorkspaceSwitch.agent_for`` for the CLI: the directory's
        ``AGENTS.md`` and skills, by the current settings."""
        from ..builder import instructions_policy, project_agent

        return project_agent(
            agent, root, self.config, instructions_policy(self.deployment)
        )

    def agent_for_remote(self, agent: Agent[Any], ref: Any) -> Agent[Any]:
        """``WorkspaceSwitch.agent_for_remote`` for the CLI: the served
        agent without the launch directory's ``AGENTS.md`` and skills."""
        from ..builder import remote_agent

        return remote_agent(agent, self.config)

    async def followups(self, request: FollowupRequest) -> Sequence[str]:
        """``create_app(followups=)`` for the CLI: the built-in suggester,
        while Settings keeps suggestions on."""
        deps = self._deps
        on = self.config.assistant.followups
        if not on or deps is None or request.agent not in deps.agents:
            return []
        model = deps.followup_model or deps.agents[request.agent].model
        if model is None:  # pragma: no cover - a served agent has a model
            return []
        return await generate_followups(request, model=model)

    # ---------------------------------------------------------- applying -

    async def apply(
        self, config: WebConfig, *, persist: bool = True, settings_only: bool = False
    ) -> None:
        """Adopt ``config``: build (validates), persist, swap the served agents.

        Ordering is the safety story: nothing is written or served until the
        new configuration proved it can build. In-flight runs keep their
        agent copies (and the run limits they started with); a replaced
        provider is retired (closed) once no live run still holds it.
        Serialized — concurrent writes apply one at a time.

        ``settings_only`` marks a write that touches no model (the
        assistant's settings): before a model is configured there is nothing
        to build or serve, so it is saved for when one arrives instead of
        refused.
        """
        async with self._lock:
            if settings_only and self.serveable(config):
                self.loaded.config = config
                if persist:
                    save_config(config, self.loaded.path)
                    self.loaded.exists = True
                return
            agents, aux_model = self.build(config)
            self.loaded.config = config
            if persist:
                save_config(config, self.loaded.path)
                self.loaded.exists = True
            deps = self._deps
            if deps is None:  # not bound yet (boot builds its own agents)
                return
            # Read when a run starts, so the next run gets these and a
            # running one keeps what it began with.
            deps.max_turns = self.max_turns()
            old = list(deps.agents.values())
            old_aux = deps.followup_model
            # In place: everything holding the registry sees the swap.
            deps.agents.clear()
            deps.agents.update(agents)
            # The fresh Subagents plugins have empty seams; give them the
            # app's supervised delivery (create_app wired only the boot ones).
            from ..subagents import _wire

            _wire(deps)
            deps.title_model = aux_model
            deps.followup_model = aux_model
            # Endpoint-window memos are keyed by (endpoint, model): a changed
            # connection must not inherit a stale probe result.
            clear_endpoint_cache()
            served = {id(a.model) for a in agents.values()}
            retired: set[int] = set()
            for agent in old:
                if id(agent.model) not in served | retired:
                    retired.add(id(agent.model))
                    self._retire(agent.model)
            # The aux provider only ever serves transient title/follow-up
            # calls, which no supervisor entry holds — the retire grace
            # period is what covers one caught mid-swap.
            if old_aux is not None and old_aux is not aux_model:
                self._retire(old_aux)
            profile = config.default_profile()
            assert profile is not None
            deps.emit(
                "config_changed",
                ConfigChangedData(
                    configured=True,
                    model=profile.model,
                    profile_id=profile.id,
                    name=profile.display_name,
                ),
            )
            log.info(
                "configuration applied: model %s (%s)",
                profile.model,
                mask_key(profile.api_key),
            )

    # ---------------------------------------------------------- retiring -

    def _retire(self, provider: object) -> None:
        """Close a replaced provider once no live run still uses it."""
        if not hasattr(provider, "aclose"):
            return
        task = asyncio.create_task(self._retire_provider(provider))
        self._retire_tasks.add(task)
        task.add_done_callback(self._retire_tasks.discard)

    async def _retire_provider(self, provider: object) -> None:
        deps = self._deps
        assert deps is not None
        waited = 0.0
        while waited < _RETIRE_TIMEOUT:
            if not self._provider_in_use(provider):
                break
            await asyncio.sleep(_RETIRE_POLL)
            waited += _RETIRE_POLL
        else:
            # A marathon run still streams on it: leak one HTTP client
            # rather than kill the run mid-flight.
            log.debug(
                "retired provider still in use after %ss; leaving open", _RETIRE_TIMEOUT
            )
            return
        # Small grace for a run that *just* finished (title/followup tasks
        # capture the model at call start).
        await asyncio.sleep(1.0)
        with suppress(Exception):
            await provider.aclose()  # type: ignore[attr-defined]

    def _provider_in_use(self, provider: object) -> bool:
        deps = self._deps
        if deps is None or deps._supervisor is None:
            return False
        return any(
            getattr(ctrl.agent, "model", None) is provider
            for _sid, ctrl in deps._supervisor
        )
