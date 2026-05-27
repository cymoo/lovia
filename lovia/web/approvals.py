"""Bridge between streamed ``ApprovalRequired`` events and ``POST /approve``.

The SSE response handler calls :meth:`ApprovalRegistry.await_decision` after
forwarding an ``ApprovalRequired`` event to the client. That call blocks the
runner's iterator until either:

* an HTTP ``POST /api/chat/approve`` resolves the future via :meth:`resolve`, or
* the stream is cancelled / released — in which case the request is denied so
  the runner can't hang.
"""

from __future__ import annotations

import asyncio

from .. import events


class ApprovalRegistry:
    """Process-local registry of pending approval futures.

    Keyed by ``(session_id, call_id)``.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], asyncio.Future[bool]] = {}
        self._lock = asyncio.Lock()

    async def await_decision(
        self, session_id: str, ev: events.ApprovalRequired
    ) -> bool:
        """Register ``ev`` and await an HTTP decision.

        Always resolves the underlying ``ApprovalRequired`` event (approve or
        reject) before returning, even on cancellation, so the runner never
        hangs waiting for a verdict.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        key = (session_id, ev.call.id)
        async with self._lock:
            self._pending[key] = fut

        decision = False
        try:
            decision = await fut
        except asyncio.CancelledError:
            # SSE disconnect or task cancellation: default-deny, then propagate.
            ev.reject()
            raise
        finally:
            async with self._lock:
                self._pending.pop(key, None)

        if decision:
            ev.approve()
        else:
            ev.reject()
        return decision

    async def resolve(self, session_id: str, call_id: str, decision: bool) -> bool:
        """Resolve a pending approval. Returns ``False`` if no match."""
        async with self._lock:
            fut = self._pending.get((session_id, call_id))
            if fut is None or fut.done():
                return False
            # Hold the lock while completing the future to avoid racing
            # release() / cancellation, which may also try to resolve it.
            fut.set_result(decision)
            return True

    async def release(self, session_id: str) -> None:
        """Default-deny any approvals still pending for ``session_id``.

        Called from the SSE handler's ``finally`` block so an early disconnect
        doesn't leave the runner blocked.
        """
        async with self._lock:
            for key, fut in list(self._pending.items()):
                if key[0] == session_id and not fut.done():
                    fut.set_result(False)
