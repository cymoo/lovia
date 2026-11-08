"""Bridge between streamed ``ApprovalRequired`` events and ``POST /approve``.

The SSE response handler calls :meth:`ApprovalRegistry.await_decision` after
forwarding an ``ApprovalRequired`` event to the client. That call blocks the
runner's iterator until either:

* an HTTP ``POST /api/chat/approve`` resolves the future via :meth:`resolve`, or
* the stream is cancelled / released — in which case the request is denied so
  the runner can't hang.

**Claiming.** An approve request that also wants the decision remembered has
work to do (write the grant) before it may wake the runner — otherwise the
next call in the same scope could ask again before the row lands. So a request
first :meth:`claim`s the pending approval: from then on it owns the decision
(a second request gets "not found", and the timeout / cancel sweeps in
:meth:`deny_pending` leave it alone) and must :meth:`resolve` it, success or
failure — the route does so in a ``finally``.
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
        # The pending request itself (for its scope) and the claims on it.
        self._pending: dict[tuple[str, str], events.ApprovalRequired] = {}
        self._claimed: set[tuple[str, str]] = set()

    async def await_decision(
        self, session_id: str, ev: events.ApprovalRequired
    ) -> Approval:
        """Register ``ev`` and await an HTTP decision; returns it.

        Registers before its first ``await`` (callers rely on that: a sweep
        issued right after this coroutine starts must see the entry). Always
        resolves the underlying ``ApprovalRequired`` event (approve or reject,
        with the decision's reason) before returning, even on cancellation,
        so the runner never hangs waiting for a verdict.
        """
        key = (session_id, ev.call.id)
        fut = self._channel.register(ev.call.id, scope=session_id)
        self._pending[key] = ev

        decision = Approval(False)
        try:
            decision = await fut
        except asyncio.CancelledError:
            # Run cancellation: default-deny, then propagate.
            ev.reject("the run was cancelled")
            raise
        finally:
            self._channel.discard(ev.call.id, scope=session_id)
            self._pending.pop(key, None)
            self._claimed.discard(key)

        if decision.approved:
            ev.approve()
        else:
            ev.reject(decision.reason)
        return decision

    def pending(self, session_id: str, call_id: str) -> events.ApprovalRequired | None:
        """The pending request, or None once decided / unknown."""
        return self._pending.get((session_id, call_id))

    def claim(self, session_id: str, call_id: str) -> bool:
        """Take ownership of a pending approval's decision.

        False when nothing is pending or another request already owns it.
        The owner must :meth:`resolve` it; until then the sweeps skip it.
        """
        key = (session_id, call_id)
        if key in self._claimed or not self._channel.is_pending(
            call_id, scope=session_id
        ):
            return False
        self._claimed.add(key)
        return True

    async def resolve(
        self, session_id: str, call_id: str, decision: bool | Approval
    ) -> bool:
        """Resolve a pending approval. Returns ``False`` if no match."""
        return self._channel.resolve(call_id, decision, scope=session_id)

    async def release(self, session_id: str, *, reason: str = "the run ended") -> None:
        """Default-deny the unclaimed approvals still pending for ``session_id``.

        Called from the SSE handler's ``finally`` block so an early disconnect
        doesn't leave the runner blocked.
        """
        self.deny_pending(session_id, reason=reason)

    def deny_pending(
        self, session_id: str, *, reason: str = "the run was cancelled"
    ) -> bool:
        """Synchronous default-deny of the unclaimed approvals pending for
        ``session_id`` (a cancel signal, the approval timeout). A claimed one
        is a decision already taken — its owner resolves it. Returns whether
        anything was denied."""
        denied = False
        for (sid, call_id), _ev in list(self._pending.items()):
            if sid == session_id and (sid, call_id) not in self._claimed:
                denied |= self._channel.resolve(
                    call_id, Approval(False, reason), scope=sid
                )
        return denied
