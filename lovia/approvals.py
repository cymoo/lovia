"""Permission vocabulary and the out-of-band channel for pending approvals.

Two things live here, deliberately together:

* **The vocabulary** every gate speaks — :data:`Decision` (``allow`` /
  ``ask`` / ``deny``), :class:`Verdict` (a decision plus *why* and *how far
  an "always allow" would reach*), and :class:`Approval` (the answer to one
  ``ask``). ``Tool.permission`` predicates and the workspace policy produce
  verdicts; the runner acts on them.
* **The channel.** The runner emits an :class:`~lovia.events.ApprovalRequired`
  event when a tool's verdict is ``ask``. Streaming consumers typically call
  ``ev.approve()`` / ``ev.reject()`` on that event — but the underlying
  decision plumbing lives in :class:`ApprovalChannel`, a separate object so
  events stay pure data and out-of-band approvers (a web request, a CLI
  prompt, another task) can resolve calls by ID without holding the event.

Each :class:`~lovia.runner.RunHandle` exposes its channel as
``handle.approvals``. Optional ``scope`` values let UI layers keep approvals
for multiple sessions in one channel.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

Decision = Literal["allow", "ask", "deny"]
"""What may happen to a call: run it, ask a human first, or refuse."""

_SEVERITY = {"allow": 0, "ask": 1, "deny": 2}


@dataclass(frozen=True)
class Verdict:
    """A policy judgement about one call: the decision, why, and its grant scope.

    ``reason`` is shown to the human when the action is ``ask`` and told to
    the model when it is ``deny``. ``scope`` names what an "always allow"
    grant would cover (``"shell:git push"``, ``"read:~/notes"``); ``None``
    means no such grant should be offered. Only meaningful on ``ask`` —
    a ``deny`` can never be granted away.
    """

    action: Decision
    reason: str = ""
    scope: str | None = None


def as_verdict(value: object) -> Verdict:
    """Normalize a permission result: a :data:`Decision` literal or a :class:`Verdict`.

    Anything else — including ``bool``, whose meaning flipped between the old
    ``needs_approval`` ("True = ask") and ``permission`` ("True reads as
    allowed") — raises ``TypeError`` so the runner can fail closed loudly
    instead of guessing.
    """
    if isinstance(value, Verdict):
        return value
    if isinstance(value, str) and value in _SEVERITY:
        return Verdict(value)  # type: ignore[arg-type]
    raise TypeError(
        f"permission must be 'allow', 'ask', 'deny' or a Verdict, got {value!r}"
    )


def merge_verdicts(*verdicts: Verdict) -> Verdict:
    """Combine verdicts, most restrictive wins.

    A ``deny`` keeps its own reason. Several ``ask`` conditions are joined
    into one reason, and the ``scope`` survives only when there is a *sole*
    ``ask``: granting one condition must never silently cover a second one
    that also needed approval.
    """
    winner = Verdict("allow")
    for verdict in verdicts:
        if _SEVERITY[verdict.action] > _SEVERITY[winner.action]:
            winner = verdict
    if winner.action == "ask":
        asks = [v for v in verdicts if v.action == "ask"]
        if len(asks) > 1:
            reasons = list(dict.fromkeys(v.reason for v in asks if v.reason))
            winner = Verdict("ask", "; ".join(reasons))
    return winner


@dataclass(frozen=True)
class Approval:
    """The answer to one approval request. Distinct from :class:`Verdict`:
    a verdict says whether to ask, an approval is what came back.

    ``reason`` travels into the tool result the model sees when the call
    was denied (the user's words, ``"approval timed out"``, ...).
    """

    approved: bool
    reason: str = ""


@dataclass
class ApprovalChannel:
    """Maps ``(scope, ToolCall.id)`` → pending ``Future[Approval]``."""

    _pending: dict[tuple[str | None, str], "asyncio.Future[Approval]"] = field(
        default_factory=dict
    )

    def register(
        self, call_id: str, *, scope: str | None = None
    ) -> "asyncio.Future[Approval]":
        """Create (or return) the future associated with ``call_id``."""
        key = (scope, call_id)
        if key in self._pending:
            return self._pending[key]
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[Approval]" = loop.create_future()
        self._pending[key] = fut
        return fut

    def approve(self, call_id: str, *, scope: str | None = None) -> None:
        """Allow the tool call. No-op if already resolved or unknown."""
        self.resolve(call_id, True, scope=scope)

    def reject(
        self, call_id: str, *, scope: str | None = None, reason: str = ""
    ) -> None:
        """Deny the tool call; ``reason`` reaches the model. No-op if already
        resolved or unknown."""
        self.resolve(call_id, Approval(False, reason), scope=scope)

    def is_pending(self, call_id: str, *, scope: str | None = None) -> bool:
        fut = self._pending.get((scope, call_id))
        return fut is not None and not fut.done()

    def resolve(
        self,
        call_id: str,
        decision: bool | Approval,
        *,
        scope: str | None = None,
    ) -> bool:
        """Resolve a pending approval. Returns ``False`` if no match exists."""
        fut = self._pending.get((scope, call_id))
        if fut is None or fut.done():
            return False
        fut.set_result(_as_approval(decision))
        return True

    def release(
        self,
        *,
        scope: str | None = None,
        decision: bool | Approval = False,
        reason: str = "",
    ) -> None:
        """Resolve every pending approval in ``scope`` with ``decision``.

        ``reason`` applies when ``decision`` is a bare ``False`` (the common
        "deny everything still pending because ..." case).
        """
        approval = _as_approval(decision)
        if reason and not approval.approved and not approval.reason:
            approval = Approval(False, reason)
        for key, fut in list(self._pending.items()):
            if key[0] == scope and not fut.done():
                fut.set_result(approval)

    def discard(self, call_id: str, *, scope: str | None = None) -> None:
        """Forget a pending approval entry after its waiter has completed."""
        self._pending.pop((scope, call_id), None)


def _as_approval(decision: bool | Approval) -> Approval:
    return decision if isinstance(decision, Approval) else Approval(bool(decision))


__all__ = [
    "Approval",
    "ApprovalChannel",
    "Decision",
    "Verdict",
    "as_verdict",
    "merge_verdicts",
]
