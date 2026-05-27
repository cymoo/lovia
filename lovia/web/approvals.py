"""Bridge between streamed ``ApprovalRequired`` events and ``POST /approve``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .. import events


@dataclass
class _Pending:
    event: events.ApprovalRequired
    future: "asyncio.Future[bool]"


class ApprovalRegistry:
    """Process-local registry of pending approval events.

    Keys are ``(session_id, call_id)``. The SSE handler calls
    :meth:`await_decision` to register the event and **block** until the
    matching ``POST /api/chat/approve`` arrives or the stream is released.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._lock = asyncio.Lock()

    async def await_decision(
        self,
        session_id: str,
        ev: events.ApprovalRequired,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Register ``ev`` and await an HTTP decision. Returns the verdict."""
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[bool]" = loop.create_future()
        key = (session_id, ev.call.id)
        async with self._lock:
            self._pending[key] = _Pending(event=ev, future=fut)
        try:
            decision = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            decision = False
        finally:
            async with self._lock:
                self._pending.pop(key, None)
        if decision:
            ev.approve()
        else:
            ev.reject()
        return decision

    async def resolve(self, session_id: str, call_id: str, decision: bool) -> bool:
        async with self._lock:
            pending = self._pending.get((session_id, call_id))
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(decision)
        return True

    async def release(self, session_id: str) -> None:
        """Default-deny any approvals still pending for ``session_id``."""
        async with self._lock:
            keys = [k for k in self._pending if k[0] == session_id]
            for k in keys:
                pending = self._pending[k]
                if not pending.future.done():
                    pending.future.set_result(False)
