"""Unit tests for ``lovia.runtime.model_turn.assemble_turn_entries``.

The function decides, per entry kind, whether to trust provider-emitted
completed entries or rebuild from streamed deltas. Each kind is independent
and all-or-nothing, so we test the matrix directly rather than through a
provider.
"""

from __future__ import annotations

import pytest

from lovia import Agent, Runner, tool
from lovia.exceptions import ProviderError
from lovia.messages import Usage
from lovia.parts import ImagePart, TextPart
from lovia.runtime.model_turn import (
    _ToolCallSlot,
    assemble_turn_entries,
    stream_with_retries,
)
from lovia.transcript import (
    IMAGE_NOT_SHOWN,
    AssistantTextEntry,
    FinishDelta,
    InputEntry,
    has_images,
    ReasoningEntry,
    TextDelta,
    ToolCallDelta,
    ToolCallEntry,
    UsageDelta,
)


def _assemble(*, text=None, reasoning=None, tool_slots=None, completed=None):
    return assemble_turn_entries(
        text=text,
        reasoning=reasoning,
        tool_slots=tool_slots or {},
        completed_entries=completed or [],
    )


def test_empty_inputs_produce_nothing() -> None:
    assert _assemble() == []


def test_text_only_from_deltas() -> None:
    out = _assemble(text="hello")
    assert out == [AssistantTextEntry(content="hello")]


def test_reasoning_only_from_deltas() -> None:
    # Delta-only reasoning fallback (no completed reasoning entry).
    out = _assemble(reasoning="thinking")
    assert out == [ReasoningEntry(content="thinking")]


def test_tool_calls_rebuilt_from_slots_sorted_by_index() -> None:
    slots = {
        1: _ToolCallSlot(call_id="c1", name="second", arguments='{"x": 1}'),
        0: _ToolCallSlot(call_id="c0", name="first", arguments=""),
    }
    out = _assemble(tool_slots=slots)
    assert [e.name for e in out] == ["first", "second"]  # type: ignore[union-attr]
    # Empty argument fragment defaults to a valid empty-object JSON string.
    assert out[0].arguments == "{}"  # type: ignore[union-attr]


def test_completed_reasoning_wins_over_delta() -> None:
    completed = [ReasoningEntry(content="from provider")]
    out = _assemble(reasoning="from delta", completed=completed)
    assert out == [ReasoningEntry(content="from provider")]


def test_completed_message_wins_over_delta_text() -> None:
    completed = [AssistantTextEntry(content="provider text")]
    out = _assemble(text="delta text", completed=completed)
    assert out == [AssistantTextEntry(content="provider text")]


def test_completed_tool_calls_win_over_slots() -> None:
    completed = [ToolCallEntry(call_id="c9", name="done", arguments="{}")]
    slots = {0: _ToolCallSlot(call_id="ignored", name="ignored", arguments="{}")}
    out = _assemble(tool_slots=slots, completed=completed)
    assert out == [ToolCallEntry(call_id="c9", name="done", arguments="{}")]


def test_full_ordering_reasoning_then_text_then_tool_calls() -> None:
    out = _assemble(
        text="answer",
        reasoning="why",
        tool_slots={0: _ToolCallSlot(call_id="c0", name="t", arguments="{}")},
    )
    assert [type(e).__name__ for e in out] == [
        "ReasoningEntry",
        "AssistantTextEntry",
        "ToolCallEntry",
    ]


def test_kinds_are_independent_completed_and_delta_mix() -> None:
    # Completed reasoning, but text + tool calls only exist as deltas.
    out = _assemble(
        text="delta text",
        reasoning="delta reasoning (ignored)",
        tool_slots={0: _ToolCallSlot(call_id="c0", name="t", arguments="{}")},
        completed=[ReasoningEntry(content="completed reasoning")],
    )
    assert out[0] == ReasoningEntry(content="completed reasoning")
    assert AssistantTextEntry(content="delta text") in out
    assert any(isinstance(e, ToolCallEntry) for e in out)


# ---- streamed tool-call fragments reassembled across deltas (via Runner) ----


class _FragmentProvider:
    """Streams a tool call in OpenAI-style fragments: the id/name arrive on
    the first chunk, then argument text dribbles in with id/name unset."""

    name = "frag"
    supports_json_schema = False

    def __init__(self) -> None:
        self.turn = 0

    async def stream(self, entries, *, tools=None, response_format=None, settings=None):
        self.turn += 1
        if self.turn == 1:
            # call_id + name set, arguments empty on the opening chunk...
            yield ToolCallDelta(index=0, call_id="c1", name="echo", arguments="")
            # ...then argument fragments with call_id/name unset.
            yield ToolCallDelta(index=0, arguments='{"msg":')
            yield ToolCallDelta(index=0, arguments=' "hi"}')
            yield UsageDelta(usage=Usage(input_tokens=1, output_tokens=1))
            yield FinishDelta(reason="tool_calls")
        else:
            yield TextDelta(text="done")
            yield UsageDelta(usage=Usage(input_tokens=1, output_tokens=1))
            yield FinishDelta(reason="stop")


@pytest.mark.asyncio
async def test_fragmented_tool_call_deltas_reassemble() -> None:
    @tool
    async def echo(msg: str) -> str:
        return f"echo:{msg}"

    agent = Agent(name="a", model=_FragmentProvider(), tools=[echo])
    result = await Runner.run(agent, "go")

    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == "echo:hi"  # fragments assembled into {"msg": "hi"}
    assert result.output == "done"


# ----------------------------- stream_with_retries --------------------------


class _Boom:
    def __init__(self, name: str) -> None:
        self.name = name

    async def stream(self, entries, *, tools=None, response_format=None, settings=None):
        raise ConnectionError(f"{self.name} down")
        yield  # pragma: no cover - unreachable, makes this an async generator


async def test_provider_error_propagates_without_retry_policy() -> None:
    with pytest.raises(ConnectionError):
        async for _ in stream_with_retries(
            _Boom("p1"), [], tools=None, response_format=None, settings=None, retry=None
        ):
            pass


async def test_cancel_token_stops_retry_backoff() -> None:
    # A cooperative cancel that lands while the retry backoff sleeps must
    # terminate the stream right there — not after sleeping out the backoff
    # and paying for another provider attempt.
    from lovia.exceptions import ProviderError, RunCancelled
    from lovia.reliability import CancelToken, RetryPolicy

    token = CancelToken()
    attempts = {"n": 0}

    class _Retryable:
        name = "retryable"

        async def stream(
            self, entries, *, tools=None, response_format=None, settings=None
        ):
            attempts["n"] += 1
            raise ProviderError("outage", retryable=True)
            yield  # pragma: no cover - makes this an async generator

    async def cancelling_sleep(_delay: float) -> None:
        token.cancel("user hit stop")

    retry = RetryPolicy(max_attempts=5, sleep=cancelling_sleep)
    with pytest.raises(RunCancelled):
        async for _ in stream_with_retries(
            _Retryable(),
            [],
            tools=None,
            response_format=None,
            settings=None,
            retry=retry,
            cancel_token=token,
        ):
            pass
    assert attempts["n"] == 1  # cancelled during the first backoff


async def test_each_retry_announces_itself_and_a_give_up_does_not() -> None:
    from lovia.events import ModelRetrying
    from lovia.exceptions import ProviderError
    from lovia.reliability import RetryPolicy

    class _Down:
        name = "down"

        async def stream(
            self, entries, *, tools=None, response_format=None, settings=None
        ):
            raise ProviderError("outage", retryable=True)
            yield  # pragma: no cover - makes this an async generator

    slept: list[float] = []

    async def _record(delay: float) -> None:
        slept.append(delay)

    seen = []
    with pytest.raises(ProviderError):
        async for d in stream_with_retries(
            _Down(),
            [],
            tools=None,
            response_format=None,
            settings=None,
            retry=RetryPolicy(max_attempts=3, sleep=_record),
        ):
            seen.append(d)
    # Attempts 1 and 2 are retried; the third failure gives up silently.
    assert [(d.attempt, d.max_attempts) for d in seen] == [(1, 3), (2, 3)]
    assert [d.delay for d in seen] == slept
    assert all(isinstance(d, ModelRetrying) and str(d.error) for d in seen)


async def test_non_retryable_after_partial_raises_without_orphan_reset() -> None:
    # restart_on_partial arms a reset when a failed attempt already streamed
    # output — but a non-retryable error means no replacing attempt follows,
    # so the armed reset must die with the raise. Leaking it would tell the
    # consumer to clear its UI for a re-stream that never comes.
    from lovia.exceptions import ProviderError
    from lovia.reliability import RetryPolicy
    from lovia.runtime.model_turn import _StreamReset

    class _PartialThenFatal:
        name = "fatal"

        async def stream(
            self, entries, *, tools=None, response_format=None, settings=None
        ):
            yield TextDelta(text="half an answer")
            raise ProviderError("bad request", retryable=False)

    async def _no_sleep(_delay: float) -> None:
        pass

    retry = RetryPolicy(max_attempts=5, sleep=_no_sleep)  # restart_on_partial on
    seen = []
    with pytest.raises(ProviderError):
        async for d in stream_with_retries(
            _PartialThenFatal(),
            [],
            tools=None,
            response_format=None,
            settings=None,
            retry=retry,
        ):
            seen.append(d)
    assert not any(isinstance(d, _StreamReset) for d in seen)


async def test_retryable_truncation_resets_and_recovers() -> None:
    # End-to-end proof of the fix: a provider that cuts off mid-tool-call on the
    # first attempt (exactly what the adapters now raise on) is retried; its
    # partial output is discarded via a single reset, and the clean re-stream
    # survives — so the half-formed call never reaches the transcript.
    from lovia.exceptions import ProviderError
    from lovia.reliability import RetryPolicy
    from lovia.runtime.model_turn import _StreamReset

    attempts = {"n": 0}

    class _TruncateOnce:
        name = "flaky"

        async def stream(
            self, entries, *, tools=None, response_format=None, settings=None
        ):
            attempts["n"] += 1
            if attempts["n"] == 1:
                yield ToolCallDelta(
                    index=0, call_id="c1", name="write_file", arguments='{"path":'
                )
                raise ProviderError("truncated response", retryable=True)
            yield ToolCallDelta(
                index=0,
                call_id="c1",
                name="write_file",
                arguments='{"path":"r.md","content":"ok"}',
            )
            yield FinishDelta(reason="tool_calls")

    async def _no_sleep(_delay: float) -> None:
        pass

    retry = RetryPolicy(max_attempts=2, sleep=_no_sleep)
    deltas = [
        d
        async for d in stream_with_retries(
            _TruncateOnce(),
            [],
            tools=None,
            response_format=None,
            settings=None,
            retry=retry,
        )
    ]

    assert attempts["n"] == 2
    # exactly one reset separates the discarded partial from the clean re-stream
    assert sum(isinstance(d, _StreamReset) for d in deltas) == 1
    tool_deltas = [d for d in deltas if isinstance(d, ToolCallDelta)]
    assert tool_deltas[-1].arguments == '{"path":"r.md","content":"ok"}'
    assert any(isinstance(d, FinishDelta) and d.reason == "tool_calls" for d in deltas)


# --- images: a model that takes none, and a refused request that carried some ---


class _Records:
    """Records what each call was sent; fails with ``error`` when given one."""

    name = "records"

    def __init__(self, *, sees: bool | None = None, error: Exception | None = None):
        if sees is not None:
            self.supports_vision = sees
        self.error = error
        self.sent: list[list] = []

    async def stream(self, entries, *, tools=None, response_format=None, settings=None):
        self.sent.append(entries)
        if self.error is not None:
            raise self.error
        yield TextDelta(text="ok")
        yield FinishDelta(reason="stop")


def _with_image() -> list:
    image = ImagePart(data="YQ==", mime_type="image/png")
    return [InputEntry(role="user", content=[TextPart("what is this?"), image])]


async def _drain(provider, entries) -> None:
    async for _ in stream_with_retries(
        provider, entries, tools=None, response_format=None, settings=None, retry=None
    ):
        pass


async def test_a_model_that_takes_no_images_is_sent_none() -> None:
    entries = _with_image()
    blind, sees, silent = _Records(sees=False), _Records(sees=True), _Records()
    for provider in (blind, sees, silent):
        await _drain(provider, entries)

    assert not has_images(blind.sent[0])
    assert blind.sent[0][0].content[1] == TextPart(IMAGE_NOT_SHOWN)
    # A provider that says nothing sees, and the transcript keeps the image
    # for the next model either way.
    assert sees.sent[0] is entries and silent.sent[0] is entries
    assert has_images(entries)


async def test_a_refused_request_that_carried_images_says_what_to_do() -> None:
    def refusal() -> ProviderError:
        return ProviderError("HTTP 400: nope", status_code=400)

    with pytest.raises(ProviderError) as carried:
        await _drain(_Records(error=refusal()), _with_image())
    assert "supports_vision=False" in (carried.value.hint or "")

    # Not when no image went out — to a model told it takes none, or in a
    # request that had none — nor for a failure that is no refusal.
    for provider, entries in (
        (_Records(sees=False, error=refusal()), _with_image()),
        (_Records(error=refusal()), [InputEntry(role="user", content="hi")]),
        (_Records(error=ProviderError("HTTP 500", status_code=500)), _with_image()),
    ):
        with pytest.raises(ProviderError) as plain:
            await _drain(provider, entries)
        assert plain.value.hint is None
