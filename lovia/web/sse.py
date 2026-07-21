"""Translate lovia stream events to SSE envelopes.

Each emitted line follows the standard ``event: <type>\\ndata: <json>\\n\\n``
shape. Sent to the wire via ``sse-starlette``'s ``EventSourceResponse`` for
correct keep-alive and disconnect semantics.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import AsyncIterator, Callable, cast

from ..types import JsonObject, JsonValue
from .. import events
from ..messages import Usage
from ..parts import text_of
from ..plugins import TodoItem
from ..transcript import (
    AssistantTextEntry,
    ReasoningEntry,
    ToolCallEntry,
    TranscriptEntry,
)


def _dumps(payload: object, default: Callable[[object], object] | None = None) -> str:
    """``json.dumps`` that keeps non-ASCII text as itself.

    The default ``ensure_ascii=True`` renders CJK and emoji as ``\\uXXXX``
    escapes, which makes the raw event stream unreadable when inspected in
    devtools. SSE is UTF-8 and only splits lines on CR/LF, so unescaped text is
    safe on the wire.
    """
    return json.dumps(payload, ensure_ascii=False, default=default)


def usage_dict(usage: Usage, *, last_input_tokens: int | None = None) -> dict[str, int]:
    """The token-usage shape shared by REST + SSE responses (and run records).

    Cache counts ride along for cost visibility (the context ring's detail
    view); ``input_tokens`` already includes them — see :class:`Usage`. Note
    ``input_tokens`` is *cumulative* across the run's model calls;
    ``last_input_tokens`` (when known) is the final call's prompt size — the
    number that describes actual context fill.
    """
    payload = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "total_tokens": usage.total_tokens,
    }
    if last_input_tokens is not None:
        payload["last_input_tokens"] = last_input_tokens
    return payload


def _todo_payload(todos: list[TodoItem]) -> list[JsonObject]:
    return [
        {"content": t.content, "status": t.status, "active_form": t.active_form}
        for t in todos
    ]


def _entries_to_dict(entries: list[TranscriptEntry]) -> JsonObject:
    """Flatten the entries emitted in one assistant turn into a wire shape.

    The web UI only needs the user-facing pieces: assistant text, the
    reasoning trace, and any tool calls the model requested.
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[JsonObject] = []
    for it in entries:
        if isinstance(it, AssistantTextEntry):
            if isinstance(it.content, str):
                text_parts.append(it.content)
        elif isinstance(it, ReasoningEntry):
            reasoning_parts.append(it.content)
        elif isinstance(it, ToolCallEntry):
            tool_calls.append(
                {"id": it.call_id, "name": it.name, "arguments": it.arguments}
            )
    return {
        "role": "assistant",
        "content": "".join(text_parts) or None,
        "reasoning": "".join(reasoning_parts) or None,
        "tool_calls": tool_calls or None,
    }


def _coerce(value: object) -> JsonValue:
    """Make non-JSON-serialisable outputs (e.g. pydantic models) safe for SSE."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        # mode="json" stringifies datetime/UUID/Decimal fields — a plain dump
        # keeps them as Python objects and json.dumps would then fail, killing
        # the SSE stream right before its `done` event.
        try:
            return cast(JsonValue, dump(mode="json"))
        except TypeError:  # a model_dump that doesn't take mode
            return cast(JsonValue, dump())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def event_to_sse(ev: events.Event) -> dict[str, str] | None:
    """Return a ``{"event": ..., "data": json}`` dict, or ``None`` to skip."""
    if isinstance(ev, events.TextDelta):
        return {"event": "text_delta", "data": _dumps({"delta": ev.delta})}
    if isinstance(ev, events.ReasoningDelta):
        return {"event": "reasoning_delta", "data": _dumps({"delta": ev.delta})}
    if isinstance(ev, events.OutputDiscarded):
        return {"event": "output_discarded", "data": "{}"}
    if isinstance(ev, events.MessageCompleted):
        return {
            "event": "message_completed",
            "data": _dumps({"message": _entries_to_dict(ev.entries)}),
        }
    if isinstance(ev, events.UserMessageInjected):
        return {
            "event": "user_injected",
            "data": _dumps({"content": text_of(ev.content), "turn": ev.turn}),
        }
    if isinstance(ev, events.ToolCallStarted):
        return {
            "event": "tool_call",
            "data": _dumps(
                {"id": ev.call.id, "name": ev.call.name, "arguments": ev.call.arguments}
            ),
        }
    if isinstance(ev, events.ToolCallCompleted):
        # A todo-plugin result is a structured list[Todo]; surface it as a
        # dedicated `todo` event the UI renders as a checklist, rather than a
        # raw tool result. Detected by type, so it works under any tool name.
        result = ev.result
        if (
            not ev.is_error
            and isinstance(result, list)
            and result
            and all(isinstance(t, TodoItem) for t in result)
        ):
            return {
                "event": "todo",
                "data": _dumps(
                    {
                        "call_id": ev.call.id,
                        "name": ev.call.name,
                        "todos": _todo_payload(result),
                    }
                ),
            }
        return {
            "event": "tool_result",
            "data": _dumps(
                {
                    "id": ev.call.id,
                    "name": ev.call.name,
                    "result": ev.output,
                    "is_error": ev.is_error,
                }
            ),
        }
    if isinstance(ev, events.ApprovalRequired):
        return {
            "event": "approval_required",
            "data": _dumps(
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
            "data": _dumps({"from": ev.from_agent.name, "to": ev.to_agent.name}),
        }
    if isinstance(ev, events.TurnStarted):
        return {
            "event": "turn_started",
            "data": _dumps({"turn": ev.turn, "agent": ev.agent.name}),
        }
    if isinstance(ev, events.ContextCompacted):
        # The notice is already JSON-safe and self-contained (reason, token
        # delta, policy-authored ``detail`` bullets, summary). Forward it whole so
        # the UI renders any policy's compaction without per-key plumbing — the
        # same shape the reload path reads back from the segment ``meta``.
        return {
            "event": "context_compacted",
            "data": _dumps({"session_id": ev.session_id, **asdict(ev.notice)}),
        }
    if isinstance(ev, (events.ToolCallFailed, events.RunFailed)):
        # Same wire shape for both: a tool-scoped error and a terminal run
        # failure render identically in the UI; terminal-ness is implied by
        # the stream ending right after a RunFailed.
        return {
            "event": "error",
            "data": _dumps({"type": type(ev.error).__name__, "message": str(ev.error)}),
        }
    if isinstance(ev, events.RunCompleted):
        return {
            "event": "done",
            # default=str: last-resort stringification for exotic values nested
            # inside a dict/list output — never lose the terminal event over
            # one unserialisable field.
            "data": _dumps(
                {
                    "output": _coerce(ev.result.output),
                    "usage": usage_dict(
                        ev.result.usage,
                        last_input_tokens=ev.result.last_input_tokens,
                    ),
                },
                default=str,
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
