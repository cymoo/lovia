"""Provider streaming and model-turn assembly."""

from __future__ import annotations

import logging
from typing import AsyncIterator

from .._types import JsonObject
from .. import events
from ..agent import Agent
from ..exceptions import ContextOverflowError
from ..messages import AssistantTurn, ToolCall, Usage
from ..output import StructuredOutput, response_format_for
from ..providers.base import ModelSettings, Provider
from ..reliability import RetryPolicy
from .state import TurnState
from .utils import truncate_repr
from ..tools import Tool
from ..tracing import Tracer
from ..transcript import (
    AssistantTextEntry,
    EntryCompletedDelta,
    FinishDelta,
    ModelDelta,
    ReasoningDelta,
    ReasoningEntry,
    TextDelta,
    ToolCallDelta,
    ToolCallEntry,
    TranscriptEntry,
    UsageDelta,
)

logger = logging.getLogger(__name__)


async def stream_model_turn(
    *,
    agent: Agent,
    providers: list[Provider],
    input_entries: list[TranscriptEntry],
    tools_by_name: dict[str, Tool],
    structured_output: StructuredOutput | None,
    tracer: Tracer,
    turn: int,
    state: TurnState,
    retry: RetryPolicy | None,
) -> AsyncIterator[events.Event]:
    """Stream one model call and capture its assembled assistant turn."""

    model_label = getattr(providers[0], "model", None) if providers else None

    text_buf: list[str] = []
    reasoning_buf: list[str] = []
    completed_entries: list[TranscriptEntry] = []
    tool_slots: dict[int, dict[str, str]] = {}
    usage = Usage()
    finish_reason: str | None = None

    with tracer.span("model_call", model=model_label, turn=turn):
        async for delta in stream_with_fallback(
            providers,
            input_entries,
            tools=[t.openai_schema() for t in tools_by_name.values()] or None,
            response_format=(
                response_format_for(structured_output)
                if structured_output and not structured_output.use_tool_fallback
                else None
            ),
            settings=agent.settings,
            retry=retry,
        ):
            if isinstance(delta, TextDelta):
                text_buf.append(delta.text)
                yield events.TextDelta(delta=delta.text)
            elif isinstance(delta, ReasoningDelta):
                reasoning_buf.append(delta.text)
                yield events.ReasoningDelta(delta=delta.text)
            elif isinstance(delta, ToolCallDelta):
                slot = tool_slots.setdefault(
                    delta.index, {"id": "", "name": "", "arguments": ""}
                )
                if delta.call_id:
                    slot["id"] = delta.call_id
                if delta.name:
                    slot["name"] = delta.name
                if delta.arguments:
                    slot["arguments"] += delta.arguments
            elif isinstance(delta, UsageDelta):
                usage = delta.usage
            elif isinstance(delta, FinishDelta):
                finish_reason = delta.reason
            elif isinstance(delta, EntryCompletedDelta):
                completed_entries.append(delta.entry)

    turn_entries = assemble_turn_entries(
        text="".join(text_buf) or None,
        reasoning="".join(reasoning_buf) or None,
        tool_slots=tool_slots,
        completed_entries=completed_entries,
    )
    state.assistant = AssistantTurn(
        content="".join(
            entry.content
            for entry in turn_entries
            if isinstance(entry, AssistantTextEntry)
        )
        or None,
        tool_calls=[
            ToolCall(id=entry.call_id, name=entry.name, arguments=entry.arguments)
            for entry in turn_entries
            if isinstance(entry, ToolCallEntry)
        ],
        usage=usage,
        finish_reason=finish_reason,
    )
    state.turn_entries = turn_entries


def assemble_turn_entries(
    *,
    text: str | None,
    reasoning: str | None,
    tool_slots: dict[int, dict[str, str]],
    completed_entries: list[TranscriptEntry],
) -> list[TranscriptEntry]:
    """Use provider-completed entries when available, with delta fallback."""

    has_reasoning = any(
        isinstance(entry, ReasoningEntry) for entry in completed_entries
    )
    has_message = any(
        isinstance(entry, AssistantTextEntry) for entry in completed_entries
    )
    has_tool_call = any(isinstance(entry, ToolCallEntry) for entry in completed_entries)

    out: list[TranscriptEntry] = []
    if has_reasoning:
        out.extend(
            entry for entry in completed_entries if isinstance(entry, ReasoningEntry)
        )
    elif reasoning:
        out.append(ReasoningEntry(content=reasoning))

    if has_message:
        out.extend(
            entry
            for entry in completed_entries
            if isinstance(entry, AssistantTextEntry)
        )
    elif text:
        out.append(AssistantTextEntry(content=text))

    if has_tool_call:
        out.extend(
            entry for entry in completed_entries if isinstance(entry, ToolCallEntry)
        )
    else:
        out.extend(
            ToolCallEntry(
                call_id=s["id"],
                name=s["name"],
                arguments=s["arguments"] or "{}",
            )
            for _, s in sorted(tool_slots.items())
        )
    return out


async def stream_with_fallback(
    providers: list[Provider],
    input_entries: list[TranscriptEntry],
    *,
    tools: list[JsonObject] | None,
    response_format: JsonObject | None,
    settings: ModelSettings | None,
    retry: RetryPolicy | None,
) -> AsyncIterator[ModelDelta]:
    """Stream from the first provider that succeeds.

    Retried/fallback errors are only safe before any delta has been forwarded.
    Once text or tool-call fragments have reached the caller, a mid-stream
    error propagates to avoid duplicating partial assistant output.
    """
    last_exc: BaseException | None = None
    max_retries = retry.max_retries if retry is not None else 1
    for provider in providers:
        attempt = 0
        while True:
            attempt += 1
            committed = False
            try:
                async for delta in provider.stream(
                    input_entries,
                    tools=tools,
                    response_format=response_format,
                    settings=settings,
                ):
                    committed = True
                    yield delta
                return
            except BaseException as exc:
                last_exc = exc
                if committed:
                    raise
                if isinstance(exc, ContextOverflowError):
                    raise
                if retry is not None and attempt < max_retries and retry.retry_on(exc):
                    import random as _random

                    delay = min(
                        retry.backoff_max, retry.backoff_base * (2 ** (attempt - 1))
                    )
                    # TODO: use better jitter strategy
                    delay *= 0.5 + _random.random()
                    logger.warning(
                        "run.retry: provider=%s attempt=%d/%d delay=%.2fs error=%s(%s)",
                        getattr(provider, "name", repr(provider)),
                        attempt,
                        max_retries,
                        delay,
                        type(exc).__name__,
                        truncate_repr(str(exc)),
                    )
                    await retry.sleep(delay)
                    continue
                break
    if last_exc is not None:
        raise last_exc
