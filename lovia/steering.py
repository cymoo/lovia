"""Mid-run input steering: a mailbox for injecting messages into a live run.

The inbound dual of :class:`~lovia.reliability.CancelToken`. Where a cancel
token signals "stop at the next safe point", a :class:`Mailbox` carries user
messages *into* a run: the runner drains it at the start of every turn (a safe
point) and appends each item as a normal ``user`` message, so the model sees it
on its next call. Whatever is still queued when the run ends is left for the
caller to feed into the next run.

Like the cancel token, a mailbox is always present on a run: the runner exposes
the caller-supplied instance — or a default it creates — as
:attr:`RunContext.mailbox <lovia.run_context.RunContext.mailbox>`, so tools and
hooks can steer the run they are part of (a deadline hook nudging "wrap up", a
plugin feeding in fresh context). Two caveats follow from drain-at-turn-start:
a push is seen on the *next* turn, never the current one; and a push during the
final turn is consumed by nobody unless the caller holds the mailbox — a
runner-created default is unreachable once the run ends.

Like :class:`CancelToken`, this is intentionally lockless and tiny. ``push`` is
a single ``list.append`` and ``drain``'s rebind-swap is atomic within one
asyncio loop (no ``await`` between the read and the rebind); even under true
threads a concurrent append lands in the captured list rather than being lost.
Safe for same-loop / single-process use — the same multi-worker caveat that
applies to the web layer's per-session state applies here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from .parts import ContentPart

# Content accepted for injection — the same shape as ``InputEntry.content`` and
# ``messages.user()``. The web layer only pushes plain ``str``; the wider type
# is free future-proofing for multimodal injection.
InjectedContent = Union[str, list[ContentPart]]


@dataclass
class Mailbox:
    """A queue of messages to inject into a running agent at turn boundaries."""

    _items: list[tuple[int, InjectedContent]] = field(default_factory=list, init=False)
    _seq: int = field(default=0, init=False)

    def push(self, content: InjectedContent) -> int:
        """Queue ``content`` for the next turn; return a token to :meth:`remove` it."""
        self._seq += 1
        self._items.append((self._seq, content))
        return self._seq

    def remove(self, token: int) -> bool:
        """Withdraw a still-queued item by its :meth:`push` token.

        Returns ``True`` if it was removed, ``False`` if it was already drained
        (consumed) or the token is unknown.
        """
        for i, (tok, _) in enumerate(self._items):
            if tok == token:
                del self._items[i]
                return True
        return False

    def drain(self) -> list[InjectedContent]:
        """Return everything queued so far and clear the mailbox (FIFO)."""
        # Rebind-swap *first* so a concurrent push lands in the fresh list rather
        # than the one we're about to return — never silently dropped (matches
        # the module docstring's lost-append guarantee).
        items, self._items = self._items, []
        return [content for _, content in items]

    def __bool__(self) -> bool:
        """True while messages are still waiting to be drained."""
        return bool(self._items)
