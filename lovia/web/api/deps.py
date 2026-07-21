"""Shared dependencies for the API routers.

A single :class:`RouterDeps` is built once in :func:`lovia.web.create_app` and
closed over by each ``build_*_router(deps)`` factory. Keeping it a plain object
(rather than wiring through ``app.state`` + ``Depends``) means a router stays
self-contained: a user can ``include_router(build_api_router(deps))`` into their
own FastAPI app with no extra plumbing — which is the point of decoupling the
API from the bundled UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

try:
    from fastapi import HTTPException
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...agent import Agent
from ...context import ContextPolicy
from ...providers import Provider
from ...reliability import CancelToken, RetryPolicy, RunBudget
from ...session import Session
from ...steering import Mailbox
from ...tracing import Tracer
from ..approvals import ApprovalRegistry
from ..store import ChatStore
from ..titles import generate_title, provisional_title

if TYPE_CHECKING:
    from ..supervisor import EventHub, RunSupervisor

log = logging.getLogger(__name__)


@dataclass
class RouterDeps:
    """Everything the API routers need, plus process-wide mutable state.

    ``cancel_tokens`` and ``_bg_tasks`` are per-process: under multiple uvicorn
    workers each process has its own copies, so a cancel issued to one worker
    won't reach a stream running on another. Run a single worker if you rely on
    cooperative stop / reconnect across requests.
    """

    agents: dict[str, Agent[Any]]
    store: ChatStore
    approvals: ApprovalRegistry
    title: str = "lovia"
    context_policy: ContextPolicy | None = None
    title_model: str | Provider | None = None
    generate_titles: bool = True
    max_turns: int = 50
    # Per-run limits. Used as a template: ``fresh_budget`` copies it per run so a
    # ``RunBudget``'s wall-clock/tool-call state never bleeds across runs.
    # ``None`` (default) leaves runs unbounded, like the core ``Runner``.
    budget: RunBudget | None = None
    retry: RetryPolicy | None = None
    tracer: Tracer | None = None
    # Cap on concurrent supervised (background) runs; over-cap interactive
    # starts are rejected (the scheduler, later, will defer).
    max_background_runs: int = 8
    # Auto-deny a pending tool approval after this many seconds (None = wait
    # forever). Without it a clientless (scheduled) run parked on an approval
    # holds one of the ``max_background_runs`` slots indefinitely.
    approval_timeout: float | None = None
    # Hard references to fire-and-forget title tasks: without these the event
    # loop only holds a weak reference and may garbage-collect a task mid-flight.
    _bg_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    # Lazily-built process-wide run supervisor (owns live runs' tasks + hubs +
    # per-session cancel token + mailbox); exposed via the ``supervisor`` prop.
    _supervisor: RunSupervisor | None = field(default=None, init=False, repr=False)
    # Lazily-built process-wide lifecycle bus behind ``GET /api/events``
    # (run started/finished, session created/retitled); see ``bus``/``emit``.
    _bus: EventHub | None = field(default=None, init=False, repr=False)

    @property
    def session(self) -> Session:
        return self.store.session

    @property
    def supervisor(self) -> RunSupervisor:
        """The process-wide run supervisor (lazily constructed)."""
        if self._supervisor is None:
            from ..supervisor import RunSupervisor

            self._supervisor = RunSupervisor(self)
        return self._supervisor

    @property
    def bus(self) -> EventHub:
        """The process-wide lifecycle event bus (lazily constructed).

        Per-process, like the supervisor: under multiple uvicorn workers each
        process fans out only its own lifecycle facts.
        """
        if self._bus is None:
            from ..supervisor import EventHub

            self._bus = EventHub()
        return self._bus

    def emit(self, event: str, **data: Any) -> None:
        """Publish one lifecycle fact to the bus, pre-encoded as an SSE dict.

        Payloads are small JSON facts (ids, status, title) — never the per-token
        stream, which stays on each run's own hub.
        """
        self.bus.publish(
            {"event": event, "data": json.dumps(data, ensure_ascii=False)}
        )

    @property
    def cancel_tokens(self) -> dict[str, CancelToken]:
        """Read-through view of live runs' cancel tokens (back-compat shim)."""
        return {sid: c.cancel for sid, c in self.supervisor}

    @property
    def mailboxes(self) -> dict[str, Mailbox]:
        """Read-through view of live runs' mailboxes (back-compat shim)."""
        return {sid: c.mailbox for sid, c in self.supervisor}

    def fresh_budget(self) -> RunBudget | None:
        """A per-run copy of ``budget`` (``None`` when unset), so a ``RunBudget``'s
        wall-clock/tool-call state never bleeds between runs — the same reason
        :func:`~lovia.handoff.agent_as_tool` copies its budget per invocation."""
        return replace(self.budget) if self.budget is not None else None

    @property
    def default_agent(self) -> str | None:
        """The implied agent when none is named — only when exactly one exists."""
        return next(iter(self.agents)) if len(self.agents) == 1 else None

    def pick(self, name: str | None) -> Agent[Any]:
        """Resolve an agent by name, defaulting to the sole agent if unambiguous."""
        if name is None:
            if len(self.agents) == 1:
                return next(iter(self.agents.values()))
            raise HTTPException(
                status_code=400,
                detail=f"agent must be specified; available: {list(self.agents)}",
            )
        if name not in self.agents:
            raise HTTPException(status_code=404, detail=f"unknown agent {name!r}")
        return self.agents[name]

    def name_of(self, agent: Agent[Any]) -> str:
        """The registry key an agent is served under — the API-facing identity.

        ``create_app({"alpha": Agent(name="bot")})`` serves the agent as
        "alpha": that key is what ``pick``/AgentInfo/session metadata all speak,
        so anything persisted or displayed must use it, never ``agent.name``
        (which is only the agent's internal self-identity, e.g. for handoffs).
        """
        for name, a in self.agents.items():
            if a is agent:
                return name
        return agent.name  # unregistered (shouldn't happen) — best effort

    def schedule_title(
        self, session_id: str, user_msg: str, output: Any, agent_name: str
    ) -> None:
        """Generate a chat title in the background; failures never propagate."""
        if not self.generate_titles:
            return
        # The provisional title the session was inserted with. The generated
        # title is only applied if this is still in place — see _run_title.
        provisional = provisional_title(user_msg).strip()[:120]
        task = asyncio.create_task(
            self._run_title(session_id, user_msg, output, agent_name, provisional)
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _run_title(
        self,
        session_id: str,
        user_msg: str,
        output: Any,
        agent_name: str,
        provisional: str,
    ) -> None:
        model = self.title_model or self.agents[agent_name].model
        if model is None:  # pragma: no cover - a just-run agent has a model
            log.warning(
                "title generation for %s skipped: agent %r has no model",
                session_id,
                agent_name,
            )
            return
        try:
            title = await generate_title(user_msg, output, model=model)
            # Compare-and-set: skip if the user renamed the chat meanwhile.
            await self.store.set_title_if_unchanged(
                session_id, title, expected=provisional
            )
            # Emit whatever title actually stuck (the generated one, or the
            # user's rename the CAS protected) so sidebars catch up either way.
            meta = await self.store.get(session_id)
            if meta is not None and meta.title:
                self.emit("session_retitled", session_id=session_id, title=meta.title)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("title generation for %s failed: %s", session_id, exc)
