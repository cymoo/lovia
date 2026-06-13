"""Provider streaming and model-turn assembly."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator

from .._types import JsonObject
from .. import events
from ..exceptions import ContextOverflowError
from ..messages import AssistantTurn, ToolCall, Usage
from ..output import StructuredOutput, response_format_for
from ..providers.base import ModelSettings, Provider
from ..reliability import RetryPolicy
from .run_state import ModelTurnResult
from .utils import truncate_repr
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

if TYPE_CHECKING:
    from ..agent import Agent
    from ..tools import Tool

logger = logging.getLogger(__name__)


@dataclass
class _ToolCallSlot:
    """One tool call assembled incrementally from streamed deltas."""

    call_id: str = ""
    name: str = ""
    arguments: str = ""


async def stream_model_turn(
    *,
    agent: Agent,
    providers: list[Provider],
    input_entries: list[TranscriptEntry],
    tools_by_name: dict[str, Tool],
    structured_output: StructuredOutput | None,
    tracer: Tracer,
    turn: int,
    result: ModelTurnResult,
    retry: RetryPolicy | None,
) -> AsyncIterator[events.Event]:
    """Stream one model call, yielding delta events.

    The assembled assistant turn lands in ``result`` (an async generator
    cannot ``return`` a value, so the caller supplies the accumulator).
    """

    model_label = getattr(providers[0], "model", None) if providers else None

    text_buf: list[str] = []
    reasoning_buf: list[str] = []
    completed_entries: list[TranscriptEntry] = []
    tool_slots: dict[int, _ToolCallSlot] = {}
    usage = Usage()
    finish_reason: str | None = None

    with tracer.span("model_call", model=model_label, turn=turn):
        async for delta in stream_with_fallback(
            providers,
            input_entries,
            tools=[t.openai_schema() for t in tools_by_name.values()] or None,
            response_format=(
                response_format_for(structured_output)
                if structured_output and structured_output.use_native
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
                slot = tool_slots.setdefault(delta.index, _ToolCallSlot())
                if delta.call_id:
                    slot.call_id = delta.call_id
                if delta.name:
                    slot.name = delta.name
                if delta.arguments:
                    slot.arguments += delta.arguments
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
    result.assistant = AssistantTurn(
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
    result.turn_entries = turn_entries


def assemble_turn_entries(
    *,
    text: str | None,
    reasoning: str | None,
    tool_slots: dict[int, _ToolCallSlot],
    completed_entries: list[TranscriptEntry],
) -> list[TranscriptEntry]:
    """Use provider-completed entries when available, with delta fallback."""

    # Per entry kind (reasoning / message / tool call) this is all-or-nothing:
    # if the provider emitted any completed entry of a kind we take only the
    # completed ones, else we rebuild that kind from streamed deltas. This
    # assumes a provider reports each kind *entirely* one way or the other; a
    # provider that mixed completed and delta-only entries of the same kind
    # would drop the delta-only ones.
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
                call_id=slot.call_id,
                name=slot.name,
                arguments=slot.arguments or "{}",
            )
            for _, slot in sorted(tool_slots.items())
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

    Each provider is retried per ``retry`` (``max_retries`` counts attempts,
    so ``None`` means a single attempt), then the next provider in the chain
    is tried. Retries and fallback are only safe before any delta has been
    forwarded; once output reached the caller, a mid-stream error propagates
    to avoid duplicating partial assistant output. Cancellation and
    :class:`ContextOverflowError` always propagate immediately.
    """
    last_exc: Exception | None = None
    max_attempts = retry.max_retries if retry is not None else 1
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
            except (asyncio.CancelledError, ContextOverflowError):
                raise
            except Exception as exc:
                last_exc = exc
                if committed:
                    raise
                if retry is not None and attempt < max_attempts and retry.retry_on(exc):
                    delay = retry.backoff_delay(attempt)
                    logger.warning(
                        "run.retry: provider=%s attempt=%d/%d delay=%.2fs error=%s(%s)",
                        getattr(provider, "name", repr(provider)),
                        attempt,
                        max_attempts,
                        delay,
                        type(exc).__name__,
                        truncate_repr(str(exc)),
                    )
                    await retry.sleep(delay)
                    continue
                break
    if last_exc is not None:
        if len(providers) > 1:
            logger.error(
                "run.fallback_exhausted: all providers failed: %s",
                ", ".join(getattr(p, "name", repr(p)) for p in providers),
            )
        raise last_exc
