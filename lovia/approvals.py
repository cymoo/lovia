"""Out-of-band channel for resolving pending tool approvals.

The runner emits an :class:`~lovia.events.ApprovalRequired` event when a
tool gated by ``needs_approval`` is about to run. Streaming consumers
typically call ``ev.approve()`` / ``ev.reject()`` on that event — but the
underlying decision plumbing lives here, in a separate **channel** so:

* Events themselves stay pure data (no futures, no side channels).
* Out-of-band approvals (from a different async task, a web request, a
  CLI prompt, ...) can resolve calls by ID without holding the event.

Each :class:`~lovia.runner.RunHandle` exposes its channel as
``handle.approvals``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class ApprovalChannel:
    """Maps ``ToolCall.id`` → pending ``Future[bool]``."""

    _pending: dict[str, "asyncio.Future[bool]"] = field(default_factory=dict)

    def register(self, call_id: str) -> "asyncio.Future[bool]":
        """Create (or return) the future associated with ``call_id``."""
        if call_id in self._pending:
            return self._pending[call_id]
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[bool]" = loop.create_future()
        self._pending[call_id] = fut
        return fut

    def approve(self, call_id: str) -> None:
        """Allow the tool call. No-op if already resolved or unknown."""
        self._resolve(call_id, True)

    def reject(self, call_id: str) -> None:
        """Deny the tool call. No-op if already resolved or unknown."""
        self._resolve(call_id, False)

    def is_pending(self, call_id: str) -> bool:
        fut = self._pending.get(call_id)
        return fut is not None and not fut.done()

    def _resolve(self, call_id: str, decision: bool) -> None:
        fut = self._pending.get(call_id)
        if fut is not None and not fut.done():
            fut.set_result(decision)
