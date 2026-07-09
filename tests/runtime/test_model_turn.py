"""Unit tests for ``lovia.runtime.model_turn.assemble_turn_entries``.

The function decides, per entry kind, whether to trust provider-emitted
completed entries or rebuild from streamed deltas. Each kind is independent
and all-or-nothing, so we test the matrix directly rather than through a
provider.
"""

from __future__ import annotations

import pytest

from lovia import Agent, Runner, tool
from lovia.messages import Usage
from lovia.runtime.model_turn import (
    _ToolCallSlot,
    assemble_turn_entries,
    stream_with_fallback,
)
from lovia.transcript import (
    AssistantTextEntry,
    FinishDelta,
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


# ----------------------------- stream_with_fallback -------------------------


class _Boom:
    def __init__(self, name: str) -> None:
        self.name = name

    async def stream(self, entries, *, tools=None, response_format=None, settings=None):
        raise ConnectionError(f"{self.name} down")
        yield  # pragma: no cover - unreachable, makes this an async generator


async def test_fallback_exhausted_raises_last_error() -> None:
    providers = [_Boom("p1"), _Boom("p2")]
    with pytest.raises(ConnectionError):
        async for _ in stream_with_fallback(
            providers, [], tools=None, response_format=None, settings=None, retry=None
        ):
            pass


async def test_fallback_with_no_providers_yields_nothing() -> None:
    out = [
        d
        async for d in stream_with_fallback(
            [], [], tools=None, response_format=None, settings=None, retry=None
        )
    ]
    assert out == []


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
        async for _ in stream_with_fallback(
            [_Retryable()],
            [],
            tools=None,
            response_format=None,
            settings=None,
            retry=retry,
            cancel_token=token,
        ):
            pass
    assert attempts["n"] == 1  # cancelled during the first backoff


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
        async for d in stream_with_fallback(
            [_TruncateOnce()],
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
