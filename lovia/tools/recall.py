"""Retrieve a full tool result that context compaction dropped from the view.

When the context policy compacts a long conversation it replaces older tool
results with a short marker in the per-call view, e.g.::

    [Earlier tool result cleared to save context.
     Call recall_tool_result("call_42") to retrieve the full output.]

Results archived to workspace files carry the same hint alongside the file
path. Either way the full output still lives in the run transcript;
``recall_tool_result`` reads it back by ``call_id`` so the agent can recover
it without re-running a tool that may have side effects or be
non-deterministic.

This tool is opt-in — add it to an agent that works with large tool outputs::

    from lovia.tools import recall_tool_result
    agent = Agent(name="x", tools=[..., recall_tool_result])
"""

from __future__ import annotations

from typing import Annotated

from ..run_context import RunContext
from ..transcript import ToolResultEntry
from .base import tool

__all__ = ["recall_tool_result"]


@tool
def recall_tool_result(
    ctx: RunContext,
    call_id: Annotated[
        str, "The call_id shown in the '[Earlier tool result omitted ...]' marker."
    ],
) -> str:
    """Return the full output of an earlier tool call by its ``call_id``."""
    for entry in reversed(ctx.entries):
        if isinstance(entry, ToolResultEntry) and entry.call_id == call_id:
            return entry.output
    return f"No tool result found for call_id {call_id!r}."
