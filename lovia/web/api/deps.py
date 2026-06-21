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
import logging
from dataclasses import dataclass, field
from typing import Any

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
from ...tracing import Tracer
from ..approvals import ApprovalRegistry
from ..store import ChatStore
from ..titles import generate_title

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
    title_model: str | Provider | list[str | Provider] | None = None
    generate_titles: bool = True
    max_turns: int = 50
    budget: RunBudget | None = None
    retry: RetryPolicy | None = None
    tracer: Tracer | None = None
    # Per-session cooperative-cancellation tokens (stop button / new stream).
    cancel_tokens: dict[str, CancelToken] = field(default_factory=dict)
    # Hard references to fire-and-forget title tasks: without these the event
    # loop only holds a weak reference and may garbage-collect a task mid-flight.
    _bg_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    @property
    def session(self) -> Session:
        return self.store.session

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

    def schedule_title(
        self, session_id: str, user_msg: str, output: Any, agent_name: str
    ) -> None:
        """Generate a chat title in the background; failures never propagate."""
        if not self.generate_titles:
            return
        task = asyncio.create_task(
            self._run_title(session_id, user_msg, output, agent_name)
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _run_title(
        self, session_id: str, user_msg: str, output: Any, agent_name: str
    ) -> None:
        model = self.title_model or self.agents[agent_name].model
        try:
            title = await generate_title(user_msg, output, model=model)
            await self.store.set_title(session_id, title)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("title generation for %s failed: %s", session_id, exc)
