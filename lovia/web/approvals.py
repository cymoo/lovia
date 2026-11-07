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
from ..approvals import Approval, ApprovalChannel


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
        reject, with the decision's reason) before returning, even on
        cancellation, so the runner never hangs waiting for a verdict.
        """
        fut = self._channel.register(ev.call.id, scope=session_id)

        decision = Approval(False)
        try:
            decision = await fut
        except asyncio.CancelledError:
            # Run cancellation: default-deny, then propagate.
            ev.reject("the run was cancelled")
            raise
        finally:
            self._channel.discard(ev.call.id, scope=session_id)

        if decision.approved:
            ev.approve()
        else:
            ev.reject(decision.reason)
        return decision.approved

    async def resolve(
        self, session_id: str, call_id: str, decision: bool | Approval
    ) -> bool:
        """Resolve a pending approval. Returns ``False`` if no match."""
        return self._channel.resolve(call_id, decision, scope=session_id)

    async def release(self, session_id: str, *, reason: str = "the run ended") -> None:
        """Default-deny any approvals still pending for ``session_id``.

        Called from the SSE handler's ``finally`` block so an early disconnect
        doesn't leave the runner blocked.
        """
        self._channel.release(scope=session_id, decision=False, reason=reason)

    def deny_pending(
        self, session_id: str, *, reason: str = "the run was cancelled"
    ) -> None:
        """Synchronous default-deny of pending approvals, for non-async callers
        (e.g. a cancel signal). Same effect as :meth:`release`, no await."""
        self._channel.release(scope=session_id, decision=False, reason=reason)
