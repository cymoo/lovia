"""Retrieve a full tool result that context compaction dropped from the view.

When the context policy compacts a long conversation it replaces older tool
results with a short marker in the per-call view, e.g.::

    [Earlier tool result cleared to save context.
     Call recall_tool_result("call_42") to retrieve the full output.]

``recall_tool_result`` reads the full output back by the reference shown in
the marker, so the agent can recover it without re-running a tool that may
have side effects or be non-deterministic. Cleared results reference their
``call_id``; offloaded results reference a **content digest** — the store is
shared across sessions while call_ids are session-local, so digests are what
keep one session's recall from ever serving another session's output (see
:class:`~lovia.context.state.OffloadRecord.digest`). Resolution order: the
policy's :class:`ResultStore`, then the run transcript by ``call_id``, then
the transcript by content digest.

The tool is **not** added by the user: a compacting :class:`ContextPolicy`
provides it via its ``tools()`` hook, bound to its own store, and the runner
injects it. :func:`make_recall_tool` is the factory the policy calls.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from ..run_context import RunContext
from ..transcript import ToolResultEntry
from .base import Tool, tool

if TYPE_CHECKING:
    from ..context.store import ResultStore

logger = logging.getLogger(__name__)

__all__ = ["make_recall_tool"]


def make_recall_tool(store: "ResultStore | None") -> Tool:
    """Build a ``recall_tool_result`` tool bound to ``store``.

    The returned tool reads ``store.get(ref)`` first, then falls back to a
    reverse scan of the transcript — by ``call_id``, then by content digest.
    ``store=None`` is the transcript-only form (used when a policy offloads
    nowhere).
    """

    @tool
    async def recall_tool_result(
        ctx: RunContext[Any],
        ref: Annotated[
            str,
            "The reference shown in the '[... tool result ...]' marker.",
        ],
    ) -> str:
        """Return the full output of an earlier tool call by the reference
        shown in its compaction marker."""
        if store is not None:
            # The store is a cache; the transcript is the source of truth. A
            # store read failure must degrade to the transcript scan, not error.
            try:
                hit = await store.get(ref)
            except Exception as exc:
                logger.warning(
                    "recall: store.get(%s) failed (%s: %s); using transcript",
                    ref,
                    type(exc).__name__,
                    exc,
                )
                hit = None
            if hit is not None:
                return hit
        for entry in reversed(ctx.entries):
            if isinstance(entry, ToolResultEntry) and entry.call_id == ref:
                return entry.output
        # Digest references (offload markers) when the store missed — an
        # ephemeral store lost to a restart, or no store at all. Hashing is
        # deferred to this last resort and matches the offload stage's
        # definition exactly (same helper).
        from ..context.state import result_digest

        for entry in reversed(ctx.entries):
            if (
                isinstance(entry, ToolResultEntry)
                and result_digest(entry.output) == ref
            ):
                return entry.output
        return f"No tool result found for reference {ref!r}."

    return recall_tool_result
