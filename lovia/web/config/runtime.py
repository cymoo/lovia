"""Runtime reconfiguration: build, serve and hot-swap the default agent.

The single choke point for "the configuration changed": every run entry
(chat, stream, reconnect, scheduler fire, subagent task) resolves its Agent
through ``RouterDeps.agents`` at message time, and in-flight runs hold
``replace()`` copies — so atomically replacing the registry entry applies a
new model to every *next* message while running turns finish undisturbed on
the old provider.

Owned by the CLI (``lovia web``): embedders configure their agents in code
and never see this class.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from ...agent import Agent
from ...context import Compaction
from ...exceptions import UserError
from ...providers import Provider, provider_from_string
from ...providers._windows import clear_endpoint_cache
from ...tools import HumanChannel
from .check import mask_key
from .schema import Connection, WebConfig
from .storage import LoadedConfig, save_config

if TYPE_CHECKING:
    from ..api.deps import RouterDeps
    from ..store import ChatStore

log = logging.getLogger("lovia.web.config")

# How long a retired provider may wait for its last run to finish before we
# give up and leak it (never close a client under a marathon run).
_RETIRE_TIMEOUT = 600.0
_RETIRE_POLL = 5.0


class ConfigRuntime:
    """Applies a :class:`WebConfig` to a running server.

    Built by the CLI with everything agent assembly needs (the argparse
    namespace for topology flags, the shared store, the ask_human channel),
    then bound to the app's ``RouterDeps`` by ``create_app``. ``apply()`` is
    the one write path: validate by building first, persist, then swap.
    """

    def __init__(
        self,
        *,
        args: argparse.Namespace,
        loaded: LoadedConfig,
        store: "ChatStore",
        question_channel: HumanChannel | None = None,
        agent_key: str | None = None,
    ) -> None:
        from ..builder import DEFAULT_AGENT_NAME

        self.args = args
        self.loaded = loaded
        self.store = store
        self.question_channel = question_channel
        self.agent_key = agent_key or DEFAULT_AGENT_NAME
        # Set by create_app when the app runs without its own auth guard —
        # the config write endpoints then insist on a loopback Host header
        # (the DNS-rebinding guard; see api/config.py).
        self.require_local_host = False
        self._deps: RouterDeps | None = None
        self._lock = asyncio.Lock()
        self._retire_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------- state -

    @property
    def config(self) -> WebConfig:
        return self.loaded.config

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

    # ---------------------------------------------------------- building -

    def build(
        self, config: WebConfig | None = None
    ) -> tuple[Agent[Any], Provider, Compaction, Provider | None]:
        """Construct the served pieces for ``config`` (no side effects).

        Returns ``(agent, provider, context_policy, followup_model)``.
        Raises :class:`UserError` when the configuration cannot serve and
        ``ValueError`` for an unknown vendor prefix — callers turn both into
        their surface's error shape.
        """
        from ..builder import (
            build_default_agent,
            resolve_followup_model,
        )

        config = config if config is not None else self.config
        missing = self.serveable(config)
        if missing:
            raise UserError(f"no {' or '.join(missing)} configured")
        profile = config.default_profile()
        assert profile is not None
        conn = Connection.from_profile(profile)
        assert conn.model is not None
        provider = provider_from_string(
            conn.model,
            api_key=conn.api_key,
            base_url=conn.base_url,
            supports_vision=profile.vision_override(),
        )
        agent = build_default_agent(
            self.args,
            self.store,
            provider,
            question_channel=self.question_channel,
            config=config,
        )
        policy = Compaction(context_window=conn.context_window)
        return agent, provider, policy, resolve_followup_model(config.aux_profile())

    # ---------------------------------------------------------- applying -

    async def apply(self, config: WebConfig, *, persist: bool = True) -> None:
        """Adopt ``config``: build (validates), persist, swap the served agent.

        Ordering is the safety story: nothing is written or served until the
        new configuration proved it can build. In-flight runs keep their
        agent copies; the old provider is retired (closed) once no live run
        still holds it. Serialized — concurrent writes apply one at a time.
        """
        async with self._lock:
            agent, provider, policy, followup_model = self.build(config)
            self.loaded.config = config
            if persist:
                save_config(config, self.loaded.path)
                self.loaded.exists = True
            deps = self._deps
            if deps is None:  # not bound yet (boot builds its own agent)
                return
            old = deps.agents.get(self.agent_key)
            deps.agents[self.agent_key] = agent
            # The fresh Subagents plugin has empty seams; give it the app's
            # supervised delivery (create_app wired only the boot-time one).
            from ..subagents import _wire

            _wire(deps)
            deps.context_policy = policy
            deps.followup_model = followup_model
            # Endpoint-window memos are keyed by (endpoint, model): a changed
            # connection must not inherit a stale probe result.
            clear_endpoint_cache()
            if old is not None and old.model is not provider:
                self._retire(old.model)
            profile = config.default_profile()
            assert profile is not None
            deps.emit(
                "config_changed",
                configured=True,
                model=profile.model,
                profile_id=profile.id,
                name=profile.display_name,
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
