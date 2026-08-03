"""Chat-scoped workspace sessions.

The core runner opens a fresh workspace session per run and closes it when the
run ends (``LocalWorkspace.close_after_run``). For a *chat*, that lifetime is
wrong for one thing only — background processes: a dev server started with
``shell(background=true)`` must outlive the turn that started it, or the
feature cannot serve its headline use case. Everything else (files, policy)
is stateless across runs and does not care.

:class:`WorkspaceSessions` scopes one live session to each web session id:
the supervisor calls :meth:`bind` when a run starts, which lazily opens the
chat's session and hands the runner a :class:`WorkspaceSessionBinding`
(``close_after_run=False``) so every run of that chat reuses it. The session
— and with it every background process — dies when the chat is deleted or
the app shuts down, never merely because a run finished.

Core semantics are untouched: processes are still session-owned and still
ephemeral across restarts; the web layer just holds sessions longer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from ..agent import Agent
from ..workspace import LocalWorkspace
from ..workspace.protocol import WorkspaceSession

log = logging.getLogger(__name__)

__all__ = ["WorkspaceSessions"]


async def _close_quietly(session_id: str, session: WorkspaceSession) -> None:
    """Best-effort close: teardown on delete/shutdown paths must not mask
    the operation the user actually asked for."""
    try:
        await session.close()
    except Exception as exc:  # noqa: BLE001 — defensive teardown
        log.warning("closing workspace session for %s failed: %s", session_id, exc)


@dataclass
class WorkspaceSessions:
    """One live workspace session per web session id (chat, task, ...).

    Uniform across run sources — user chats, scheduled runs, and subagent
    task sessions each get their own entry keyed by their session id, and
    every deletion path funnels through :meth:`close`. Per-process, like the
    supervisor: under multiple uvicorn workers each process holds its own
    sessions.
    """

    _live: dict[str, tuple[LocalWorkspace, WorkspaceSession]] = field(
        default_factory=dict
    )
    # Serializes opens so two concurrent runs of one chat share a session
    # instead of racing two into existence (the loser would leak unclosed).
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def bind(self, session_id: str, agent: Agent[Any]) -> Agent[Any]:
        """Return ``agent`` with its workspace bound to the chat's session.

        Only plain :class:`LocalWorkspace` configs are intercepted. ``None``
        and custom ``WorkspaceLike`` backends pass through untouched, and so
        does an agent the caller already bound to a session of its own —
        their lifetimes are not this registry's to manage.
        """
        cfg = agent.workspace
        if not isinstance(cfg, LocalWorkspace):
            return agent
        async with self._lock:
            held = self._live.get(session_id)
            if held is not None and not (held[0] is cfg or held[0] == cfg):
                # The chat switched to an agent with a different workspace
                # config (root/policy): the old session must not keep serving
                # — close it (killing its background processes) and reopen.
                await _close_quietly(session_id, held[1])
                held = None
            if held is None:
                held = (cfg, await cfg.open())
                self._live[session_id] = held
        return replace(agent, workspace=cfg.bind(held[1]))

    def get(self, session_id: str) -> WorkspaceSession | None:
        """The chat's live session, or None (no run bound one yet)."""
        held = self._live.get(session_id)
        return held[1] if held is not None else None

    async def close(self, session_id: str) -> None:
        """Close and drop the chat's session (reaps its background processes).

        Idempotent; a later :meth:`bind` simply opens a fresh session.
        """
        async with self._lock:
            held = self._live.pop(session_id, None)
        if held is not None:
            await _close_quietly(session_id, held[1])

    async def aclose(self) -> None:
        """Close every live session (app shutdown)."""
        async with self._lock:
            live, self._live = self._live, {}
        for session_id, (_, session) in live.items():
            await _close_quietly(session_id, session)
