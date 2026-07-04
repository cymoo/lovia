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
from ..approvals import ApprovalChannel


class ApprovalRegistry:
    """Session-scoped adapter around :class:`lovia.ApprovalChannel`.

    Keyed by ``(session_id, call_id)`` for HTTP resolution.
    """

    def __init__(self) -> None:
        self._channel = ApprovalChannel()

    async def await_decision(
        self, session_id: str, ev: events.ApprovalRequired
    ) -> bool:
        """Register ``ev`` and await an HTTP decision.

        Always resolves the underlying ``ApprovalRequired`` event (approve or
        reject) before returning, even on cancellation, so the runner never
        hangs waiting for a verdict.
        """
        fut = self._channel.register(ev.call.id, scope=session_id)

        decision = False
        try:
            decision = await fut
        except asyncio.CancelledError:
            # SSE disconnect or task cancellation: default-deny, then propagate.
            ev.reject()
            raise
        finally:
            self._channel.discard(ev.call.id, scope=session_id)

        if decision:
            ev.approve()
        else:
            ev.reject()
        return decision

    async def resolve(self, session_id: str, call_id: str, decision: bool) -> bool:
        """Resolve a pending approval. Returns ``False`` if no match."""
        return self._channel.resolve(call_id, decision, scope=session_id)

    async def release(self, session_id: str) -> None:
        """Default-deny any approvals still pending for ``session_id``.

        Called from the SSE handler's ``finally`` block so an early disconnect
        doesn't leave the runner blocked.
        """
        self._channel.release(scope=session_id, decision=False)

    def deny_pending(self, session_id: str) -> None:
        """Synchronous default-deny of pending approvals, for non-async callers
        (e.g. a cancel signal). Same effect as :meth:`release`, no await."""
        self._channel.release(scope=session_id, decision=False)
