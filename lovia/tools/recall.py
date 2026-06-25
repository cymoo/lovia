"""Retrieve a full tool result that context compaction dropped from the view.

When the context policy compacts a long conversation it replaces older tool
results with a short marker in the per-call view, e.g.::

    [Earlier tool result cleared to save context.
     Call recall_tool_result("call_42") to retrieve the full output.]

``recall_tool_result`` reads the full output back by ``call_id`` so the agent
can recover it without re-running a tool that may have side effects or be
non-deterministic. It looks first in the policy's :class:`ResultStore` (where
offloaded results are archived) and falls back to the run transcript (where
cleared results — and offloaded ones, until trimming lands — still live).

The tool is **not** added by the user: a compacting :class:`ContextPolicy`
provides it via its ``tools()`` hook, bound to its own store, and the runner
injects it. :func:`make_recall_tool` is the factory the policy calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from ..run_context import RunContext
from ..transcript import ToolResultEntry
from .base import Tool, tool

if TYPE_CHECKING:
    from ..context.store import ResultStore

__all__ = ["make_recall_tool"]


def make_recall_tool(store: "ResultStore | None") -> Tool:
    """Build a ``recall_tool_result`` tool bound to ``store``.

    The returned tool reads ``store.get(call_id)`` first, then falls back to a
    reverse scan of the transcript. ``store=None`` is the entries-only form
    (used when a policy offloads nowhere).
    """

    @tool
    async def recall_tool_result(
        ctx: RunContext[Any],
        call_id: Annotated[
            str,
            "The call_id shown in the '[Earlier tool result ...]' marker.",
        ],
    ) -> str:
        """Return the full output of an earlier tool call by its ``call_id``."""
        if store is not None:
            hit = await store.get(call_id)
            if hit is not None:
                return hit
        for entry in reversed(ctx.entries):
            if isinstance(entry, ToolResultEntry) and entry.call_id == call_id:
                return entry.output
        return f"No tool result found for call_id {call_id!r}."

    return recall_tool_result
