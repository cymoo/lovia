"""Provider streaming and model-turn assembly."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

from ..types import JsonObject
from .. import events
from ..exceptions import ContextOverflowError
from ..messages import AssistantTurn, ToolCall, Usage
from ..output import StructuredOutput, response_format_for
from ..providers.base import ModelSettings, Provider
from ..reliability import CancelToken, RetryPolicy
from .run_state import ModelTurnResult
from .utils import truncate_repr
from ..tracing import Tracer, model_call_span
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


@dataclass
class _StreamReset:
    """Internal signal: discard partial output assembled so far and restart.

    Yielded by :func:`stream_with_retries` when it retries after the provider
    already streamed part of a turn. Not a provider-emitted delta and never
    leaves this module — :func:`stream_model_turn` clears its accumulation
    buffers on receipt so the re-streamed turn is not duplicated.

    ``visible`` is ``True`` when the discarded attempt produced user-facing
    text or reasoning; only then does the runner surface an
    :class:`~lovia.events.OutputDiscarded` event. A tool-call-only attempt
    pollutes the internal buffers but shows nothing, so it resets silently.
    """

    visible: bool = False


async def stream_model_turn(
    *,
    agent: Agent[Any],
    provider: Provider,
    input_entries: list[TranscriptEntry],
    tools_by_name: dict[str, Tool],
    structured_output: StructuredOutput | None,
    tracer: Tracer,
    turn: int,
    result: ModelTurnResult,
    retry: RetryPolicy | None,
    cancel_token: CancelToken | None = None,
) -> AsyncIterator[events.Event]:
    """Stream one model call, yielding delta events.

    The assembled assistant turn lands in ``result`` (an async generator
    cannot ``return`` a value, so the caller supplies the accumulator).
    """

    model_label = getattr(provider, "model", None)

    text_buf: list[str] = []
    reasoning_buf: list[str] = []
    completed_entries: list[TranscriptEntry] = []
    tool_slots: dict[int, _ToolCallSlot] = {}
    usage = Usage()
    finish_reason: str | None = None

    with model_call_span(tracer, model=model_label, turn=turn):
        async for delta in stream_with_retries(
            provider,
            input_entries,
            tools=[t.openai_schema() for t in tools_by_name.values()] or None,
            response_format=(
                response_format_for(structured_output)
                if structured_output and structured_output.use_native
                else None
            ),
            settings=agent.settings,
            retry=retry,
            cancel_token=cancel_token,
        ):
            if isinstance(delta, _StreamReset):
                # A failed attempt streamed partial output; discard everything
                # accumulated for this turn so the re-stream replaces it rather
                # than appending. Surface OutputDiscarded only when the user
                # actually saw text/reasoning (tool-call-only resets silently).
                text_buf.clear()
                reasoning_buf.clear()
                completed_entries.clear()
                tool_slots.clear()
                usage = Usage()
                finish_reason = None
                if delta.visible:
                    yield events.OutputDiscarded()
                continue
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


async def stream_with_retries(
    provider: Provider,
    input_entries: list[TranscriptEntry],
    *,
    tools: list[JsonObject] | None,
    response_format: JsonObject | None,
    settings: ModelSettings | None,
    retry: RetryPolicy | None,
    cancel_token: CancelToken | None = None,
) -> AsyncIterator[ModelDelta | _StreamReset]:
    """Stream one model call, retrying transient failures per ``retry``.

    ``max_attempts`` counts attempts, so ``retry=None`` means a single one.
    When ``retry.restart_on_partial`` is set (the default), a failure *after*
    output has been forwarded is also recovered: a :class:`_StreamReset` is
    yielded so the caller discards the partial output, and the turn is
    re-streamed from scratch (replace semantics). With ``restart_on_partial``
    off, a mid-stream error propagates immediately. Cancellation and
    :class:`ContextOverflowError` always propagate immediately.

    ``cancel_token``, when supplied, is checked around each retry backoff so a
    cooperatively canceled run stops here instead of sleeping out the backoff
    and paying for another attempt before the loop's next safe point.
    """
    max_attempts = retry.max_attempts if retry is not None else 1
    restart_on_partial = retry.restart_on_partial if retry is not None else False
    # Set when a failed attempt already streamed output and another attempt
    # will follow; flushed once at the start of that next attempt so each
    # real restart emits exactly one reset.
    pending_reset: _StreamReset | None = None
    attempt = 0
    while True:
        attempt += 1
        if pending_reset is not None:
            yield pending_reset
            pending_reset = None
        produced_any = False
        produced_visible = False
        try:
            async for delta in provider.stream(
                input_entries,
                tools=tools,
                response_format=response_format,
                settings=settings,
            ):
                produced_any = True
                if isinstance(delta, (TextDelta, ReasoningDelta)):
                    produced_visible = True
                yield delta
            return
        except (asyncio.CancelledError, ContextOverflowError):
            raise
        except Exception as exc:
            if produced_any and not restart_on_partial:
                logger.warning(
                    "run.retry_skipped: mid-stream error after partial output,"
                    " not retrying provider=%s error=%s(%s)",
                    getattr(provider, "name", repr(provider)),
                    type(exc).__name__,
                    truncate_repr(str(exc)),
                )
                raise
            # A retry will replace whatever this attempt streamed. Armed here,
            # emitted at the start of that next attempt — so a final give-up
            # never emits an orphan reset.
            if produced_any:
                pending_reset = _StreamReset(visible=produced_visible)
            if retry is None or attempt >= max_attempts or not retry.retry_on(exc):
                raise
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
            # Checked on both sides of the sleep: before, so an
            # already-canceled run skips the backoff entirely; after,
            # so a cancel that lands mid-sleep stops the retry rather
            # than paying for one more full attempt.
            if cancel_token is not None:
                cancel_token.check()
            await retry.sleep(delay)
            if cancel_token is not None:
                cancel_token.check()
