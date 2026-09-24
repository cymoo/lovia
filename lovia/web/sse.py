"""Translate lovia stream events to SSE envelopes.

Each emitted line follows the standard ``event: <type>\\ndata: <json>\\n\\n``
shape. Sent to the wire via ``sse-starlette``'s ``EventSourceResponse`` for
correct keep-alive and disconnect semantics.

The wire protocol is the TypedDicts below: :data:`CHAT_EVENTS` for the chat
streams, :data:`LIFECYCLE_EVENTS` for ``GET /api/events``. The HTTP API docs
tables are checked against both registries (``tests/web/test_protocol.py``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import AsyncIterator, Callable, TypedDict, cast

from ..types import JsonObject, JsonValue
from .. import events
from ..exceptions import LoviaError, ProviderError
from ..messages import Usage
from ..parts import ImagePart, coerce_parts, text_of
from ..plugins import TodoItem
from ..transcript import (
    AssistantTextEntry,
    ReasoningEntry,
    ToolCallEntry,
    TranscriptEntry,
)
from .errors import RunErrorCode, run_error_code

# ---- chat stream payloads (POST /api/chat/stream, /api/chat/reconnect) ----


class SessionData(TypedDict):
    session_id: str


class SnapshotData(TypedDict):
    session_id: str
    status: str
    entries: list[JsonObject]


class DeltaData(TypedDict):
    delta: str


class EmptyData(TypedDict):
    pass


class MessageCompletedData(TypedDict):
    message: JsonObject


class UserInjectedData(TypedDict):
    content: str
    turn: int


class ToolCallData(TypedDict):
    id: str
    name: str
    arguments: str


class _ToolResultRequired(TypedDict):
    id: str
    name: str
    result: str
    is_error: bool


class ToolResultData(_ToolResultRequired, total=False):
    images: list[JsonObject]


class TodoData(TypedDict):
    call_id: str
    name: str
    todos: list[JsonObject]


HandoffData = TypedDict("HandoffData", {"from": str, "to": str})


class TurnStartedData(TypedDict):
    turn: int
    agent: str


class ContextCompactedData(TypedDict):
    """``session_id`` plus the :class:`~lovia.events.CompactionNotice` fields."""

    session_id: str | None
    reason: str
    reactive: bool
    summary: str | None
    tokens_before: int | None
    tokens_after: int | None
    detail: list[str]


class _ErrorRequired(TypedDict):
    type: str
    message: str
    code: RunErrorCode


class ErrorData(_ErrorRequired, total=False):
    """``status_code``/``retryable`` come from a provider error; ``hint`` from
    any :class:`~lovia.exceptions.LoviaError` that carries one."""

    status_code: int
    retryable: bool
    hint: str


class DoneData(TypedDict):
    output: JsonValue
    usage: dict[str, int]


CHAT_EVENTS: dict[str, type] = {
    "session": SessionData,
    "snapshot": SnapshotData,
    "text_delta": DeltaData,
    "reasoning_delta": DeltaData,
    "output_discarded": EmptyData,
    "message_completed": MessageCompletedData,
    "user_injected": UserInjectedData,
    "tool_call": ToolCallData,
    "tool_result": ToolResultData,
    "todo": TodoData,
    "approval_required": ToolCallData,
    "handoff": HandoffData,
    "turn_started": TurnStartedData,
    "context_compacted": ContextCompactedData,
    "error": ErrorData,
    "done": DoneData,
}

# ---- lifecycle payloads (GET /api/events) ----------------------------------


class RunStartedData(TypedDict):
    session_id: str
    run_id: str
    agent: str
    source: str


class RunFinishedData(TypedDict):
    session_id: str
    run_id: str
    status: str
    error: str | None
    source: str


class SessionCreatedData(TypedDict):
    session_id: str
    agent: str | None
    title: str | None


class SessionRetitledData(TypedDict):
    session_id: str
    title: str | None


class ConfigChangedData(TypedDict):
    configured: bool
    model: str
    profile_id: str
    name: str


LIFECYCLE_EVENTS: dict[str, type] = {
    "run_started": RunStartedData,
    "run_finished": RunFinishedData,
    "session_created": SessionCreatedData,
    "session_retitled": SessionRetitledData,
    "config_changed": ConfigChangedData,
}


def frame(
    event: str,
    data: Mapping[str, object],
    default: Callable[[object], object] | None = None,
) -> dict[str, str]:
    """One ``{"event", "data"}`` envelope, as ``EventSourceResponse`` sends it."""
    return {"event": event, "data": _dumps(data, default)}


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
        return frame("text_delta", DeltaData(delta=ev.delta))
    if isinstance(ev, events.ReasoningDelta):
        return frame("reasoning_delta", DeltaData(delta=ev.delta))
    if isinstance(ev, events.OutputDiscarded):
        return frame("output_discarded", EmptyData())
    if isinstance(ev, events.MessageCompleted):
        return frame(
            "message_completed",
            MessageCompletedData(message=_entries_to_dict(ev.entries)),
        )
    if isinstance(ev, events.UserMessageInjected):
        return frame(
            "user_injected",
            UserInjectedData(content=text_of(ev.content), turn=ev.turn),
        )
    if isinstance(ev, events.ToolCallStarted):
        return frame(
            "tool_call",
            ToolCallData(id=ev.call.id, name=ev.call.name, arguments=ev.call.arguments),
        )
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
            return frame(
                "todo",
                TodoData(
                    call_id=ev.call.id, name=ev.call.name, todos=_todo_payload(result)
                ),
            )
        payload = ToolResultData(
            id=ev.call.id, name=ev.call.name, result=ev.output, is_error=ev.is_error
        )
        # Image parts ride as byte-free stubs; the client fetches the pixels
        # from the tool-images route (0-based over the result's image parts).
        parts = None if ev.is_error else coerce_parts(result)
        if parts:
            images: list[JsonObject] = [
                {"index": i, "mime_type": p.mime_type}
                for i, p in enumerate(p for p in parts if isinstance(p, ImagePart))
            ]
            if images:
                payload["images"] = images
        return frame("tool_result", payload)
    if isinstance(ev, events.ApprovalRequired):
        return frame(
            "approval_required",
            ToolCallData(id=ev.call.id, name=ev.call.name, arguments=ev.call.arguments),
        )
    if isinstance(ev, events.HandoffOccurred):
        return frame(
            "handoff", HandoffData({"from": ev.from_agent.name, "to": ev.to_agent.name})
        )
    if isinstance(ev, events.TurnStarted):
        return frame("turn_started", TurnStartedData(turn=ev.turn, agent=ev.agent.name))
    if isinstance(ev, events.ContextCompacted):
        # Forwarded whole, so a field added to CompactionNotice reaches the UI
        # without plumbing — the protocol test keeps ContextCompactedData in step.
        return frame(
            "context_compacted",
            cast(
                ContextCompactedData,
                {"session_id": ev.session_id, **asdict(ev.notice)},
            ),
        )
    if isinstance(ev, (events.ToolCallFailed, events.RunFailed)):
        return frame("error", error_data(ev))
    if isinstance(ev, events.RunCompleted):
        # default=str: last-resort stringification for exotic values nested
        # inside a dict/list output — never lose the terminal event over one
        # unserialisable field.
        return frame(
            "done",
            DoneData(
                output=_coerce(ev.result.output),
                usage=usage_dict(
                    ev.result.usage, last_input_tokens=ev.result.last_input_tokens
                ),
            ),
            default=str,
        )
    return None


def error_data(ev: events.ToolCallFailed | events.RunFailed) -> ErrorData:
    """The ``error`` payload — one wire shape for a tool-scoped failure and a
    terminal one (terminal-ness is the stream ending right after)."""
    exc = ev.error
    # Message-less exceptions (a bare ConnectError from a dropped connection)
    # stringify to "" — fall back to the class name, same as the runner's own
    # "Tool error: ..." string. A LoviaError's hint moves to its own field.
    if isinstance(exc, LoviaError):
        message, hint = Exception.__str__(exc), exc.hint
    else:
        message, hint = str(exc), None
    code: RunErrorCode = (
        "tool_error" if isinstance(ev, events.ToolCallFailed) else run_error_code(exc)
    )
    data = ErrorData(
        type=type(exc).__name__, message=message or type(exc).__name__, code=code
    )
    if isinstance(exc, ProviderError):
        if exc.status_code is not None:
            data["status_code"] = exc.status_code
        if exc.retryable is not None:
            data["retryable"] = exc.retryable
    if hint:
        data["hint"] = hint
    return data


async def encode_stream(
    source: AsyncIterator[events.Event],
) -> AsyncIterator[dict[str, str]]:
    """Adapt a lovia event stream into an SSE-ready dict iterator."""
    async for ev in source:
        payload = event_to_sse(ev)
        if payload is not None:
            yield payload
