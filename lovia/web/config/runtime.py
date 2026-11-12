"""Runtime reconfiguration: build, serve and hot-swap the configured agents.

The single choke point for "the configuration changed": every run entry
(chat, stream, reconnect, scheduler fire, subagent task) resolves its Agent
through ``RouterDeps.agents`` at message time, and in-flight runs hold
``replace()`` copies — so atomically replacing the registry applies a new
configuration to every *next* message while running turns finish undisturbed
on the old providers.

Owned by the CLI (``lovia web``): embedders configure their agents in code
and never see this class.
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
from ...tools import HumanChannel
from ...workspace import LocalWorkspace
from ..followups import FollowupRequest, generate_followups
from ..sse import ConfigChangedData
from .check import mask_key
from .schema import AgentConfig, Connection, WebConfig
from .storage import LoadedConfig, config_writable, save_config

if TYPE_CHECKING:
    from ..api.deps import RouterDeps
    from ..builder import Deployment
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

    Each configured agent is served under its ``id``, which is also its core
    ``Agent.name``; :meth:`agent_config` maps a served agent (or its
    background child) back to the settings it runs on.
    """

    def __init__(
        self,
        *,
        deployment: "Deployment | None" = None,
        loaded: LoadedConfig,
        store: "ChatStore",
        question_channel: HumanChannel | None = None,
    ) -> None:
        from ..builder import Deployment

        self.deployment = deployment if deployment is not None else Deployment()
        self.loaded = loaded
        self.store = store
        self.question_channel = question_channel
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

    def agent_config(self, name: str) -> AgentConfig | None:
        """The settings behind a served agent's ``Agent.name`` (or its
        ``<id>.sub`` child), ``None`` when no configured agent has it."""
        from ..builder import spec_id

        return self.config.agent(spec_id(name))

    def _spec(self, agent: Agent[Any]) -> AgentConfig:
        # An agent deleted since the run began runs on as the default does.
        return self.agent_config(agent.name) or self.config.default_agent

    # ---------------------------------------------------------- building -

    def build(
        self, config: WebConfig | None = None
    ) -> tuple[dict[str, Agent[Any]], Provider | None]:
        """Construct the served pieces for ``config`` (no side effects but
        the one Memory plugin, built on first need and reused by every build).

        Returns ``(agents, aux_model)``: every configured agent by id, in
        order (the first is the default), and the aux model that serves
        titles and follow-ups, or ``None`` for "the agent's own". Agents on
        one model share its provider; agents that keep memory share the
        Memory plugin. The CLI boot and ``apply`` both build through here,
        so a field can't reach one path and miss the other. Raises
        :class:`UserError` when the configuration cannot serve (naming the
        agent whose model is incomplete) and ``ValueError`` for an unknown
        vendor prefix — callers turn both into their surface's error shape.
        """
        from ..builder import build_agent, resolve_aux_model, resolve_memory

        config = config if config is not None else self.config
        missing = self.serveable(config)
        if missing:
            raise UserError(f"no {' or '.join(missing)} configured")
        providers: dict[str, Provider] = {}
        memory = None
        if any(spec.memory for spec in config.agents):
            if self._memory is None:
                self._memory = resolve_memory(True, self.deployment.home / "memory")
            memory = self._memory
        agents: dict[str, Agent[Any]] = {}
        for spec in config.agents:
            profile = config.agent_profile(spec)
            assert profile is not None  # references are validated
            provider = providers.get(profile.id)
            if provider is None:
                conn = Connection.from_profile(profile)
                gaps = conn.missing()
                if gaps or conn.model is None:
                    raise UserError(
                        f"agent {spec.display_name!r}: no {' or '.join(gaps)} "
                        f"configured for model {profile.display_name!r}"
                    )
                provider = provider_from_string(
                    conn.model,
                    api_key=conn.api_key,
                    base_url=conn.base_url,
                    supports_vision=profile.vision_override(),
                    extra_body=profile.extra_body,
                )
                providers[profile.id] = provider
            agents[spec.id] = build_agent(
                self.deployment,
                self.store,
                provider,
                question_channel=self.question_channel,
                config=config,
                spec=spec,
                memory=memory,
            )
        return agents, resolve_aux_model(config.aux_profile())

    def max_turns(self) -> dict[str, int]:
        """Each served agent's turn limit — ``create_app(max_turns=)`` —
        and its background child's, which runs under its own name."""
        from ..builder import SUBAGENT_SUFFIX

        limits: dict[str, int] = {}
        for spec in self.config.agents:
            limits[spec.id] = limits[f"{spec.id}{SUBAGENT_SUFFIX}"] = spec.max_turns
        return limits

    def workspace_for(self, agent: Agent[Any], root: Path, mode: str) -> LocalWorkspace:
        """A chat's workspace in ``mode`` — ``WorkspaceSwitch.build`` for the
        CLI, with the auto-allowed commands of the agent the chat runs on.
        Built from the current configuration each run, so a change to them
        reaches every coding chat's next run."""
        from ..builder import resolve_workspace

        workspace = resolve_workspace(
            self.deployment, self._spec(agent), root=root, mode=mode
        )
        assert workspace is not None  # a switch exists only with a workspace
        return workspace

    def agent_for(self, agent: Agent[Any], root: Path) -> Agent[Any]:
        """``WorkspaceSwitch.agent_for`` for the CLI: the directory's
        ``AGENTS.md`` and skills, by the agent's current settings."""
        from ..builder import project_agent

        return project_agent(agent, root, self.config, self._spec(agent))

    def agent_for_remote(self, agent: Agent[Any], ref: Any) -> Agent[Any]:
        """``WorkspaceSwitch.agent_for_remote`` for the CLI: the served
        agent without the launch directory's ``AGENTS.md`` and skills."""
        from ..builder import remote_agent

        return remote_agent(agent, self.config, self._spec(agent))

    async def followups(self, request: FollowupRequest) -> Sequence[str]:
        """``create_app(followups=)`` for the CLI: the built-in suggester,
        for the agents that keep suggestions on."""
        spec = self.agent_config(request.agent)
        deps = self._deps
        if spec is None or not spec.followups or deps is None:
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

        ``settings_only`` marks a write that touches no model (an agent's
        settings): before a model is configured there is nothing to build
        or serve, so it is saved for when one arrives instead of refused.
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
                "configuration applied: %d agent(s), default model %s (%s)",
                len(agents),
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
