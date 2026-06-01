"""Translate lovia stream events to SSE envelopes.

Each emitted line follows the standard ``event: <type>\\ndata: <json>\\n\\n``
shape. Sent to the wire via ``sse-starlette``'s ``EventSourceResponse`` for
correct keep-alive and disconnect semantics.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, AsyncIterator

from pydantic import BaseModel

from .. import events
from ..items import Item, MessageOutputItem, ReasoningItem, ToolCallItem


class _ModelEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return obj.model_dump()
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        return super().default(obj)


def _items_to_dict(items: list[Item]) -> dict[str, Any]:
    """Flatten the items emitted in one assistant turn into a wire shape.

    The web UI only needs the user-facing pieces: assistant text, the
    reasoning trace, and any tool calls the model requested.
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, str]] = []
    for it in items:
        if isinstance(it, MessageOutputItem):
            if isinstance(it.content, str):
                text_parts.append(it.content)
        elif isinstance(it, ReasoningItem):
            reasoning_parts.append(it.content)
        elif isinstance(it, ToolCallItem):
            tool_calls.append(
                {"id": it.call_id, "name": it.name, "arguments": it.arguments}
            )
    return {
        "role": "assistant",
        "content": "".join(text_parts) or None,
        "reasoning": "".join(reasoning_parts) or None,
        "tool_calls": tool_calls or None,
    }


def _format_result(value: Any) -> str:
    """Format a tool result as a human-readable string for the web UI.

    Pydantic models are rendered as ``key: value`` lines so that actual
    newlines inside string fields (e.g. ``CommandResult.stdout``) survive
    JSON round-tripping and display correctly in the browser's ``<pre>``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        lines: list[str] = []
        for k, v in value.model_dump().items():
            if isinstance(v, str):
                lines.append(f"{k}:\n{v.rstrip()}" if "\n" in v else f"{k}: {v}")
            else:
                lines.append(f"{k}: {v}")
        return "\n".join(lines)
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False, cls=_ModelEncoder)
    return str(value)


def _coerce(value: Any) -> Any:
    """Make non-JSON-serialisable outputs (e.g. pydantic models) safe for SSE."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def event_to_sse(ev: events.Event) -> dict[str, str] | None:
    """Return a ``{"event": ..., "data": json}`` dict, or ``None`` to skip."""
    if isinstance(ev, events.TextDelta):
        return {"event": "text_delta", "data": json.dumps({"delta": ev.delta})}
    if isinstance(ev, events.ReasoningDelta):
        return {"event": "reasoning_delta", "data": json.dumps({"delta": ev.delta})}
    if isinstance(ev, events.MessageCompleted):
        return {
            "event": "message_completed",
            "data": json.dumps({"message": _items_to_dict(ev.items)}),
        }
    if isinstance(ev, events.ToolCallStarted):
        return {
            "event": "tool_call",
            "data": json.dumps(
                {"id": ev.call.id, "name": ev.call.name, "arguments": ev.call.arguments}
            ),
        }
    if isinstance(ev, events.ToolCallCompleted):
        return {
            "event": "tool_result",
            "data": json.dumps(
                {
                    "id": ev.call.id,
                    "name": ev.call.name,
                    "result": _format_result(ev.result),
                    "is_error": ev.is_error,
                }
            ),
        }
    if isinstance(ev, events.ApprovalRequired):
        return {
            "event": "approval_required",
            "data": json.dumps(
                {
                    "id": ev.call.id,
                    "name": ev.call.name,
                    "arguments": ev.call.arguments,
                }
            ),
        }
    if isinstance(ev, events.HandoffOccurred):
        return {
            "event": "handoff",
            "data": json.dumps({"from": ev.from_agent.name, "to": ev.to_agent.name}),
        }
    if isinstance(ev, events.ErrorOccurred):
        return {
            "event": "error",
            "data": json.dumps(
                {"type": type(ev.error).__name__, "message": str(ev.error)}
            ),
        }
    if isinstance(ev, events.RunCompleted):
        return {
            "event": "done",
            "data": json.dumps(
                {
                    "output": _coerce(ev.result.output),
                    "usage": {
                        "input_tokens": ev.result.usage.input_tokens,
                        "output_tokens": ev.result.usage.output_tokens,
                        "total_tokens": ev.result.usage.total_tokens,
                    },
                }
            ),
        }
    return None


async def encode_stream(
    source: AsyncIterator[events.Event],
) -> AsyncIterator[dict[str, str]]:
    """Adapt a lovia event stream into an SSE-ready dict iterator."""
    async for ev in source:
        payload = event_to_sse(ev)
        if payload is not None:
            yield payload
