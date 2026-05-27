"""Translate lovia stream events to SSE envelopes.

Each emitted line follows the standard ``event: <type>\\ndata: <json>\\n\\n``
shape. Sent to the wire via ``sse-starlette``'s ``EventSourceResponse`` for
correct keep-alive and disconnect semantics.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from .. import events
from ..messages import ChatMessage


def _msg_to_dict(msg: ChatMessage) -> dict[str, Any]:
    return {
        "role": msg.role,
        "content": msg.text or None,
        "tool_calls": [
            {"id": c.id, "name": c.name, "arguments": c.arguments}
            for c in (msg.tool_calls or [])
        ]
        or None,
    }


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
            "data": json.dumps({"message": _msg_to_dict(ev.message)}),
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
                    "result": str(ev.result),
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
