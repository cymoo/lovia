"""Tests for the ContextPolicy stack."""

from __future__ import annotations

import pytest

from lovia import (
    Agent,
    ArchiveEvent,
    ContextOverflowError,
    InputEntry,
    AssistantTextEntry,
    NoopContextPolicy,
    Runner,
    SummarizingContextPolicy,
    ToolCallEntry,
    ToolResultEntry,
    safe_window,
)
from lovia.context_policy import PolicyContext, extract_compaction_summary
from lovia.events import ContextCompacted
from lovia.stores.memory import InMemorySession

from .scripted_provider import ScriptedProvider, text


# ---------------------------------------------------------------------------
# safe_window
# ---------------------------------------------------------------------------


def _user(s: str) -> InputEntry:
    return InputEntry(role="user", content=s)


def _call(call_id: str, name: str = "f") -> ToolCallEntry:
    return ToolCallEntry(call_id=call_id, name=name, arguments="{}")


def _out(call_id: str, content: str = "ok") -> ToolResultEntry:
    return ToolResultEntry(call_id=call_id, output=content)


def test_safe_window_simple_slice():
    entries = [_user(f"m{i}") for i in range(10)]
    got = safe_window(entries, tail=3)
    assert [it.content for it in got] == ["m7", "m8", "m9"]


def test_safe_window_returns_full_when_tail_exceeds_length():
    entries = [_user("a"), _user("b")]
    assert safe_window(entries, tail=5) == entries


def test_safe_window_with_head_and_tail():
    entries = [_user(f"m{i}") for i in range(10)]
    got = safe_window(entries, head=2, tail=3)
    assert [it.content for it in got] == ["m0", "m1", "m7", "m8", "m9"]


def test_extract_compaction_summary() -> None:
    entries = [
        InputEntry(
            role="system",
            content=(
                "[Conversation summary — prior turns compacted]\n\n"
                "Important state.\n\n"
                "[End summary]"
            ),
        )
    ]

    assert extract_compaction_summary(entries) == "Important state."
    assert extract_compaction_summary([_user("plain")]) is None


def test_safe_window_pulls_orphan_tool_call_into_tail():
    """Tail starts on a tool_result whose call is in the dropped middle."""
    entries = [
        _user("u0"),
        _user("u1"),
        _call("c1"),
        _out("c1", "result-1"),
        _user("u2"),
    ]
    # Tail=2 would slice [_out("c1"), _user("u2")] which is invalid; the
    # helper must expand to also include the matching tool_call.
    got = safe_window(entries, tail=2)
    assert got == entries[2:]


def test_safe_window_drops_orphan_when_call_missing():
    """No matching tool_call exists anywhere → drop the orphan output."""
    entries = [_user("u0"), _out("missing", "result"), _user("u1")]
    got = safe_window(entries, tail=2)
    assert got == [_user("u1")]


def test_safe_window_pair_in_head_does_not_pull_back():
    entries = [_call("c1"), _user("u0"), _user("u1"), _out("c1")]
    got = safe_window(entries, head=1, tail=1)
    # head keeps the call; tail kept the output; no expansion needed.
    assert got == [_call("c1"), _out("c1")]


# ---------------------------------------------------------------------------
# NoopContextPolicy
# ---------------------------------------------------------------------------


async def test_noop_policy_returns_same_list_object():
    policy = NoopContextPolicy()
    entries = [_user("hi")]
    out = await policy.apply(entries, ctx=PolicyContext(provider=None, model=None))
    assert out is entries
    out2 = await policy.apply_reactive(
        entries, ctx=PolicyContext(provider=None, model=None)
    )
    assert out2 is entries


# ---------------------------------------------------------------------------
# SummarizingContextPolicy: unit-level
# ---------------------------------------------------------------------------


class _FakeSummarizer:
    def __init__(self, text: str = "SUMMARY_TEXT") -> None:
        self.text = text
        self.calls: list[list] = []

    async def summarize(self, entries, *, ctx):
        self.calls.append(list(entries))
        return self.text


class _FailingSummarizer:
    async def summarize(self, entries, *, ctx):
        raise RuntimeError("boom")


class _FakeProviderWithWindow:
    """A stand-in provider that just answers context_window queries."""

    name = "fake"

    def __init__(self, *, window: int | None = 1000) -> None:
        self.model = "fake-model"
        self._window = window

    def context_window(self, model: str) -> int | None:
        return self._window


async def test_summarizing_skips_when_under_threshold():
    summarizer = _FakeSummarizer()
    policy = SummarizingContextPolicy(
        max_tokens=10_000,
        compact_at_ratio=0.8,
        summarizer=summarizer,
    )
    entries = [_user("short")]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(),
        model="fake-model",
        last_prompt_tokens=100,
    )
    out = await policy.apply(entries, ctx=ctx)
    assert out is entries
    assert summarizer.calls == []


async def test_summarizing_compacts_when_over_threshold():
    summarizer = _FakeSummarizer("Goal: ship feature.")
    archived: list[ArchiveEvent] = []

    async def archive(ev: ArchiveEvent) -> None:
        archived.append(ev)

    policy = SummarizingContextPolicy(
        max_tokens=1_000,
        compact_at_ratio=0.5,  # threshold = 500
        keep_recent_messages=2,
        summarizer=summarizer,
        archive=archive,
    )
    entries = [_user(f"m{i}") for i in range(10)]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(window=1_000),
        model="fake-model",
        last_prompt_tokens=900,  # over threshold
        session_id="sess-1",
    )
    out = await policy.apply(entries, ctx=ctx)
    assert out is not entries
    head = out[0]
    assert isinstance(head, InputEntry)
    assert "Goal: ship feature." in head.content
    # keep_recent_messages=2 → summary + last 2 originals
    assert out[1:] == entries[-2:]
    # Archive received the full before snapshot.
    assert len(archived) == 1
    assert archived[0].session_id == "sess-1"
    assert archived[0].summary == "Goal: ship feature."
    assert archived[0].reactive is False


async def test_summarizing_falls_back_to_provider_context_window():
    summarizer = _FakeSummarizer()
    policy = SummarizingContextPolicy(
        max_tokens=None,  # let provider answer
        compact_at_ratio=0.5,
        summarizer=summarizer,
    )
    entries = [_user("x" * 100) for _ in range(10)]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(window=1_000),
        model="fake-model",
        last_prompt_tokens=600,
    )
    out = await policy.apply(entries, ctx=ctx)
    assert out is not entries
    assert summarizer.calls  # summarizer was invoked


async def test_summarizing_skips_when_no_window_info_available():
    summarizer = _FakeSummarizer()
    policy = SummarizingContextPolicy(
        max_tokens=None,
        summarizer=summarizer,
    )
    entries = [_user("x") for _ in range(10)]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(window=None),
        model="unknown-model",
        last_prompt_tokens=999_999,
    )
    out = await policy.apply(entries, ctx=ctx)
    # Without window info, proactive compaction is disabled.
    assert out is entries
    assert summarizer.calls == []


async def test_summarizing_reactive_always_compacts():
    summarizer = _FakeSummarizer("Reactive summary.")
    policy = SummarizingContextPolicy(
        max_tokens=None,
        summarizer=summarizer,
        reactive_keep_recent_messages=1,
    )
    entries = [_user(f"m{i}") for i in range(5)]
    ctx = PolicyContext(provider=None, model=None)
    out = await policy.apply_reactive(entries, ctx=ctx)
    assert out is not entries
    assert len(out) == 2  # summary + 1 tail
    assert isinstance(out[0], InputEntry)
    assert "Reactive summary." in out[0].content


async def test_summarizing_micro_compact_replaces_old_outputs():
    summarizer = _FakeSummarizer()
    policy = SummarizingContextPolicy(
        max_tokens=10_000_000,  # never proactively summarize
        keep_recent_tool_results=1,
        summarizer=summarizer,
    )
    entries = [
        _call("c1"),
        _out("c1", "first-result " * 50),
        _call("c2"),
        _out("c2", "second-result " * 50),
        _call("c3"),
        _out("c3", "third-result " * 50),
    ]
    ctx = PolicyContext(provider=_FakeProviderWithWindow(), model="fake-model")
    out = await policy.apply(entries, ctx=ctx)
    assert out is not entries
    # First two outputs replaced, last one preserved.
    assert "compacted" in out[1].output.lower()
    assert "compacted" in out[3].output.lower()
    assert "third-result" in out[5].output


async def test_summarizing_circuit_breaker():
    failing = _FailingSummarizer()
    policy = SummarizingContextPolicy(
        max_tokens=100,
        compact_at_ratio=0.5,
        summarizer=failing,
        max_consecutive_failures=2,
    )
    entries = [_user("x" * 1000)]
    ctx = PolicyContext(
        provider=None,
        model=None,
        last_prompt_tokens=500,
    )
    # First two attempts propagate the underlying error.
    with pytest.raises(RuntimeError, match="boom"):
        await policy.apply(entries, ctx=ctx)
    with pytest.raises(RuntimeError, match="boom"):
        await policy.apply(entries, ctx=ctx)
    # Third call: breaker tripped → returns entries unchanged, no exception.
    out = await policy.apply(entries, ctx=ctx)
    assert out is entries


async def test_summarizing_uses_current_entries_when_last_prompt_is_stale():
    """Regression: ``last_prompt_tokens`` is the *previous* turn's prompt
    size — it does not include the assistant reply, tool results, or new
    user message that have been appended since. The policy must therefore
    fall back to the current entries estimate when it is larger; otherwise a
    big tool result silently overshoots the model's hard cap before the
    next ``usage`` count arrives.
    """
    summarizer = _FakeSummarizer("compacted")
    policy = SummarizingContextPolicy(
        max_tokens=1_000,
        compact_at_ratio=0.5,  # threshold = 500
        keep_recent_messages=2,
        summarizer=summarizer,
    )
    # 10 messages of ~400 chars each ≈ 1000 estimated tokens, well above
    # threshold. ``last_prompt_tokens`` is stale and below threshold.
    entries = [_user("x" * 400) for _ in range(10)]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(window=1_000),
        model="fake-model",
        last_prompt_tokens=100,  # stale: from a much earlier turn
    )
    out = await policy.apply(entries, ctx=ctx)
    assert out is not entries, (
        "expected compaction to trigger from current-entries estimate "
        "despite stale last_prompt_tokens"
    )
    assert summarizer.calls


# ---------------------------------------------------------------------------
# Runner integration
# ---------------------------------------------------------------------------


class _OverflowOnceProvider:
    """Raises ContextOverflowError on the first call, then behaves normally."""

    name = "overflow-once"

    def __init__(self, model: str = "fake-model") -> None:
        self.model = model
        self.stream_count = 0
        self.last_input_lengths: list[int] = []

    def context_window(self, model: str) -> int | None:
        return 10_000_000  # never trigger proactive path

    async def stream(self, entries, *, tools=None, response_format=None, settings=None):
        self.stream_count += 1
        self.last_input_lengths.append(len(entries))
        if self.stream_count == 1:
            raise ContextOverflowError("simulated overflow")
        # Yield a normal assistant reply.
        from lovia.transcript import FinishDelta, TextDelta, UsageDelta
        from lovia.messages import Usage

        yield TextDelta(text="hello after compaction")
        yield UsageDelta(usage=Usage(input_tokens=10, output_tokens=2))
        yield FinishDelta(reason="stop")


async def test_runner_reactive_compaction_recovers_from_overflow():
    summarizer = _FakeSummarizer("Compacted history.")
    policy = SummarizingContextPolicy(
        max_tokens=None,
        summarizer=summarizer,
        reactive_keep_recent_messages=1,
    )
    provider = _OverflowOnceProvider()
    agent = Agent(
        name="t",
        instructions="be brief",
        model=provider,
    )
    result = await Runner.run(
        agent,
        "hello there",
        context_policy=policy,
    )
    # Provider was called twice: once raised, once succeeded.
    assert provider.stream_count == 2
    # The summarizer was invoked once (reactive path).
    assert len(summarizer.calls) == 1
    # The final result reflects the post-compaction reply.
    assert "hello after compaction" in (result.output or "")


async def test_runner_emits_context_compacted_event():
    summarizer = _FakeSummarizer("S.")
    policy = SummarizingContextPolicy(
        max_tokens=None,
        summarizer=summarizer,
    )
    provider = _OverflowOnceProvider()
    agent = Agent(
        name="t",
        instructions="x",
        model=provider,
    )
    events_seen: list = []
    async for ev in Runner.stream(agent, "go", context_policy=policy):
        events_seen.append(ev)
    compacted = [e for e in events_seen if isinstance(e, ContextCompacted)]
    assert len(compacted) == 1
    assert compacted[0].reactive is True
    assert compacted[0].summary == "S."


async def test_runner_no_policy_keeps_existing_behavior():
    """Sanity: omitting context_policy doesn't alter normal runs."""
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", instructions="x", model=provider)
    result = await Runner.run(agent, "ping")
    assert result.output == "hi"


async def test_runner_session_replace_after_compaction():
    summarizer = _FakeSummarizer("S.")
    policy = SummarizingContextPolicy(
        max_tokens=None,
        summarizer=summarizer,
        reactive_keep_recent_messages=1,
    )
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    sess = InMemorySession()
    await Runner.run(
        agent,
        "first",
        context_policy=policy,
        session=sess,
        session_id="s1",
    )
    persisted = await sess.load("s1")
    # The first entry should be the summary marker, not the original "first".
    assert isinstance(persisted[0], InputEntry)
    assert "Conversation summary" in persisted[0].content
    # The final assistant reply must also be persisted.
    assert any(
        isinstance(it, AssistantTextEntry) and "hello after compaction" in it.content
        for it in persisted
    )
