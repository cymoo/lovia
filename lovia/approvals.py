"""Out-of-band channel for resolving pending tool approvals.

The runner emits an :class:`~lovia.events.ApprovalRequired` event when a
tool gated by ``needs_approval`` is about to run. Streaming consumers
typically call ``ev.approve()`` / ``ev.reject()`` on that event — but the
underlying decision plumbing lives here, in a separate **channel** so:

* Events themselves stay pure data (no futures, no side channels).
* Out-of-band approvals (from a different async task, a web request, a
  CLI prompt, ...) can resolve calls by ID without holding the event.

Each :class:`~lovia.runner.RunHandle` exposes its channel as
``handle.approvals``. Optional ``scope`` values let UI layers keep approvals
for multiple sessions in one channel.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class ApprovalChannel:
    """Maps ``(scope, ToolCall.id)`` → pending ``Future[bool]``."""

    _pending: dict[tuple[str | None, str], "asyncio.Future[bool]"] = field(
        default_factory=dict
    )

    def register(
        self, call_id: str, *, scope: str | None = None
    ) -> "asyncio.Future[bool]":
        """Create (or return) the future associated with ``call_id``."""
        key = (scope, call_id)
        if key in self._pending:
            return self._pending[key]
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[bool]" = loop.create_future()
        self._pending[key] = fut
        return fut

    def approve(self, call_id: str, *, scope: str | None = None) -> None:
        """Allow the tool call. No-op if already resolved or unknown."""
        self.resolve(call_id, True, scope=scope)

    def reject(self, call_id: str, *, scope: str | None = None) -> None:
        """Deny the tool call. No-op if already resolved or unknown."""
        self.resolve(call_id, False, scope=scope)

    def is_pending(self, call_id: str, *, scope: str | None = None) -> bool:
        fut = self._pending.get((scope, call_id))
        return fut is not None and not fut.done()

    def resolve(
        self, call_id: str, decision: bool, *, scope: str | None = None
    ) -> bool:
        """Resolve a pending approval. Returns ``False`` if no match exists."""
        fut = self._pending.get((scope, call_id))
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    def release(self, *, scope: str | None = None, decision: bool = False) -> None:
        """Resolve every pending approval in ``scope`` with ``decision``."""
        for key, fut in list(self._pending.items()):
            if key[0] == scope and not fut.done():
                fut.set_result(decision)

    def discard(self, call_id: str, *, scope: str | None = None) -> None:
        """Forget a pending approval entry after its waiter has completed."""
        self._pending.pop((scope, call_id), None)
