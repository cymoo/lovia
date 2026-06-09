"""Tests for the ContextPolicy stack."""

from __future__ import annotations

import pytest

from lovia import (
    Agent,
    AssistantTextEntry,
    CompactingContextPolicy,
    ContextOverflowError,
    InputEntry,
    NoopContextPolicy,
    Runner,
    ToolCallEntry,
    ToolResultEntry,
    safe_window,
)
from lovia.context import CompactionRequest
from lovia.events import ContextCompacted
from lovia.run_context import RunContext
from lovia.stores.memory import InMemorySession
from lovia.tools import recall_tool_result, run_tool

from ..scripted_provider import ScriptedProvider, text


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
# Edge cases: degenerate inputs
# ---------------------------------------------------------------------------


def test_safe_window_tail_zero_returns_head_only():
    entries = [_user("a"), _user("b"), _user("c")]
    assert safe_window(entries, tail=0) == []
    assert safe_window(entries, head=2, tail=0) == [_user("a"), _user("b")]


def test_safe_window_tail_negative_returns_head_only():
    entries = [_user("a"), _user("b")]
    assert safe_window(entries, head=1, tail=-1) == [_user("a")]


def test_safe_window_head_negative_clamped_to_zero():
    entries = [_user("a"), _user("b"), _user("c")]
    got = safe_window(entries, head=-5, tail=2)
    assert got == [_user("b"), _user("c")]


def test_safe_window_empty_list():
    assert safe_window([], tail=1) == []


def test_safe_window_single_entry():
    entries = [_user("only")]
    assert safe_window(entries, tail=1) == entries
    assert safe_window(entries, tail=2) == entries  # tail > n


def test_safe_window_head_plus_tail_exceeds_length_with_tools():
    """When head+tail covers everything, return full list without orphan checks."""
    entries = [_user("u0"), _call("c1"), _out("c1"), _user("u1")]
    got = safe_window(entries, head=2, tail=3)
    assert got == entries


# ---------------------------------------------------------------------------
# Orphan resolution: core algorithm
# ---------------------------------------------------------------------------


def test_safe_window_tail_starts_at_call_boundary_no_orphans():
    """Tail aligns with a ToolCallEntry — no orphans, no expansion."""
    entries = [
        _user("u0"),
        _user("u1"),
        _call("c1"),
        _out("c1", "result"),
    ]
    got = safe_window(entries, tail=2)
    assert got == [_call("c1"), _out("c1", "result")]


def test_safe_window_multiple_orphans_single_pass():
    """Two orphan results whose calls are both in the dropped middle."""
    entries = [
        _user("u0"),
        _call("c1"),
        _call("c2"),
        _out("c1", "r1"),
        _out("c2", "r2"),
    ]
    # tail=2 → [out(c1), out(c2)] — both are orphans.
    # Scan backward finds call(c2) then call(c1), pulls cut to call(c1)'s index.
    got = safe_window(entries, tail=2)
    assert got == [_call("c1"), _call("c2"), _out("c1", "r1"), _out("c2", "r2")]


def test_safe_window_cross_dependency_fixed_point():
    """
    Call(B)
    Call(A)
    Result(B)
    Result(A)

    Pass 1: tail includes Result(A).  Orphan = {A}.  Expand to include Call(A).
            Now tail also includes Result(B).  Orphan = {B}.
    Pass 2: Expand again to include Call(B).  Done.
    """
    entries = [
        _user("u0"),
        _call("cB"),
        _call("cA"),
        _out("cB", "rB"),
        _out("cA", "rA"),
        _user("uX"),
    ]
    got = safe_window(entries, tail=2)
    # Should include everything from call(cB) onward.
    assert got == [
        _call("cB"),
        _call("cA"),
        _out("cB", "rB"),
        _out("cA", "rA"),
        _user("uX"),
    ]


def test_safe_window_chained_expansion_three_passes():
    """
    Call(C)  ← pulled in pass 3
    Call(B)  ← pulled in pass 2
    Call(A)  ← pulled in pass 1
    Result(C)
    Result(B)
    Result(A)
    """
    entries = [
        _call("cC"),
        _call("cB"),
        _call("cA"),
        _out("cC", "rC"),
        _out("cB", "rB"),
        _out("cA", "rA"),
    ]
    got = safe_window(entries, tail=1)
    # tail=1 → [Result(A)].  Need Call(A), which exposes Result(B),
    # need Call(B), which exposes Result(C), need Call(C).
    assert got == entries


def test_safe_window_partial_resolution_some_orphans_unresolvable():
    """Mix of resolvable and unresolvable orphans in the same tail."""
    entries = [
        _user("u0"),
        _call("c_good"),
        _out("c_bad", "orphan_without_call"),
        _out("c_good", "result"),
        _user("uX"),
    ]
    got = safe_window(entries, tail=3)
    # tail=3 → [out(c_bad), out(c_good), uX]
    # c_good is resolvable (call at index 1), c_bad is not.
    # Expected: call(c_good) pulled in, out(c_bad) dropped.
    assert got == [
        _call("c_good"),
        _out("c_good", "result"),
        _user("uX"),
    ]


def test_safe_window_all_tool_entries_no_plain_text():
    """Transcript consisting entirely of tool calls and results."""
    entries = [
        _call("c1"),
        _out("c1", "r1"),
        _call("c2"),
        _out("c2", "r2"),
        _call("c3"),
        _out("c3", "r3"),
    ]
    got = safe_window(entries, tail=3)
    # tail=3 → [out(c2), call(c3), out(c3)]
    # out(c2) is orphan → pull call(c2).
    # Result: [call(c2), out(c2), call(c3), out(c3)]
    assert got == [
        _call("c2"),
        _out("c2", "r2"),
        _call("c3"),
        _out("c3", "r3"),
    ]


# ---------------------------------------------------------------------------
# Head interaction
# ---------------------------------------------------------------------------


def test_safe_window_head_zero_expansion_to_zero():
    """Expansion pulls cut all the way to index 0 — should return full list."""
    entries = [
        _call("c1"),
        _out("c1", "r1"),
        _user("u0"),
        _user("u1"),
    ]
    got = safe_window(entries, head=0, tail=2)
    # tail=2 → [u0, u1] — no orphans, just a simple slice.
    assert got == [_user("u0"), _user("u1")]


def test_safe_window_expansion_reaches_head_boundary():
    """Expansion pulls cut <= head, triggering full-list fallback."""
    entries = [
        _user("sys"),
        _user("greeting"),
        _call("c1"),
        _user("mid"),
        _out("c1", "result"),
        _user("end"),
    ]
    got = safe_window(entries, head=2, tail=2)
    # head=2 keeps [sys, greeting], head_call_ids = {}
    # tail=2 → [out(c1), end], orphan = {c1}
    # Scan backward: call(c1) at index 2, cut becomes 2.
    # cut(2) <= head(2) → return full list.
    assert got == entries


def test_safe_window_call_in_head_no_expansion():
    """Call is in the head slice; head_call_ids protects orphan detection."""
    entries = [
        _call("c1"),
        _user("u0"),
        _out("c1", "r1"),
    ]
    got = safe_window(entries, head=1, tail=1)
    # head=[call(c1)], head_call_ids={"c1"}
    # tail=[out(c1)], orphan={} because "c1" in head_call_ids.
    assert got == [_call("c1"), _out("c1", "r1")]


def test_safe_window_head_zero_is_common_case():
    """head=0 is how CompactingContextPolicy calls safe_window."""
    entries = [
        _user("u0"),
        _call("c1"),
        _out("c1", "r1"),
        _user("u1"),
        _user("u2"),
    ]
    got = safe_window(entries, tail=3)
    # tail=3 → [out(c1), u1, u2], orphan={c1} → pull call(c1).
    assert got == [_call("c1"), _out("c1", "r1"), _user("u1"), _user("u2")]


# ---------------------------------------------------------------------------
# Malformed / edge-case transcripts
# ---------------------------------------------------------------------------


def test_safe_window_result_before_call_dropped():
    """A ToolResultEntry appears before its ToolCallEntry — illegal but don't crash."""
    entries = [
        _out("c1", "result_before_call"),
        _user("u0"),
        _call("c1"),
    ]
    got = safe_window(entries, tail=2)
    # tail=2 → [u0, call(c1)].  No ToolResultEntry in tail → no orphans.
    assert got == [_user("u0"), _call("c1")]


def test_safe_window_result_before_call_in_tail():
    """Orphan result whose call is to its right (illegal ordering)."""
    entries = [
        _user("u0"),
        _out("c1", "result_before_call"),
        _call("c1"),
        _user("uX"),
    ]
    got = safe_window(entries, tail=2)
    # tail=2 → [call(c1), uX].  No orphans (call is in tail).
    assert got == [_call("c1"), _user("uX")]


def test_safe_window_duplicate_results_same_call_id():
    """Multiple ToolResultEntrys for the same call_id."""
    entries = [
        _user("u0"),
        _call("c1"),
        _out("c1", "first"),
        _out("c1", "second"),
        _user("uX"),
    ]
    got = safe_window(entries, tail=3)
    # tail=3 → [out(c1, first), out(c1, second), uX]
    # orphan={c1} → pulls call(c1).
    assert got == [
        _call("c1"),
        _out("c1", "first"),
        _out("c1", "second"),
        _user("uX"),
    ]


def test_safe_window_orphan_at_very_start():
    """Orphan ToolResultEntry at index 0 — no call can be found before it."""
    entries = [
        _out("orphan", "no_call_exists"),
        _user("u0"),
        _user("u1"),
    ]
    got = safe_window(entries, tail=2)
    assert got == [_user("u0"), _user("u1")]


def test_safe_window_single_orphan_result_without_any_call():
    """Orphan result whose call doesn't exist anywhere — must be dropped.

    Needs head+tail < n so the orphan is actually in the tail region
    (otherwise head+tail >= n returns the full list immediately)."""
    entries = [
        _user("u0"),
        _out("ghost", "no call anywhere"),
        _user("u1"),
    ]
    got = safe_window(entries, tail=2)
    # tail=2 → [out(ghost), u1], orphan={ghost}
    # Scan backward finds only u0 → unresolvable → dropped.
    assert got == [_user("u1")]


# ---------------------------------------------------------------------------
# Mixed-content scenarios
# ---------------------------------------------------------------------------


def test_safe_window_interleaved_tools_and_text():
    """Realistic transcript: alternating user messages and tool calls."""
    entries = [
        _user("question 1"),
        _call("search", "search"),
        _out("search", "results"),
        InputEntry(role="assistant", content="Here is the answer."),
        _user("question 2"),
        _call("calc", "calc"),
        _out("calc", "42"),
        InputEntry(role="assistant", content="The answer is 42."),
    ]
    got = safe_window(entries, tail=4)
    # tail=4 → [call(calc), out(calc), assistant, assistant]
    # call(calc) is in tail, out(calc) is in tail → no orphans.
    assert got == entries[4:]


def test_safe_window_long_sequence():
    """Stress test: many alternating calls and results."""
    entries: list = [_user("start")]
    for i in range(20):
        entries.append(_call(f"c{i}"))
        entries.append(_out(f"c{i}", f"result-{i}"))
    entries.append(_user("end"))

    got = safe_window(entries, tail=5)
    # Last 5 entries should include some call/result pairs.
    # Check no orphan ToolResultEntrys remain.
    call_ids_in_result = {it.call_id for it in got if isinstance(it, ToolCallEntry)}
    for it in got:
        if isinstance(it, ToolResultEntry):
            assert it.call_id in call_ids_in_result, (
                f"Orphan ToolResultEntry {it.call_id} in result"
            )


# ---------------------------------------------------------------------------
# Test helpers (new API)
# ---------------------------------------------------------------------------


class _FakeSummarizer:
    def __init__(self, text: str = "SUMMARY_TEXT") -> None:
        self.text = text
        self.calls: list[list] = []
        self.priors: list[str | None] = []

    async def summarize(self, entries, *, req, prior_summary=None):
        self.calls.append(list(entries))
        self.priors.append(prior_summary)
        return self.text


class _FailingSummarizer:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, entries, *, req, prior_summary=None):
        self.calls += 1
        raise RuntimeError("boom")


class _FakeProviderWithWindow:
    """A stand-in provider that just answers context_window queries."""

    name = "fake"

    def __init__(self, *, window: int | None = 1000) -> None:
        self.model = "fake-model"
        self._window = window

    def context_window(self, model: str) -> int | None:
        return self._window


def _req(entries, **kw):
    return CompactionRequest(entries=entries, **kw)


# ---------------------------------------------------------------------------
# NoopContextPolicy
# ---------------------------------------------------------------------------


async def test_noop_policy_returns_same_list_object():
    policy = NoopContextPolicy()
    entries = [_user("hi")]
    out = await policy.compact(_req(entries, provider=None, model=None))
    assert out.entries is entries
    assert out.changed is False


# ---------------------------------------------------------------------------
# CompactingContextPolicy: view-only, never mutates input
# ---------------------------------------------------------------------------


async def test_compacting_skips_when_under_threshold():
    summarizer = _FakeSummarizer()
    policy = CompactingContextPolicy(
        window_tokens=10_000,
        trigger_ratio=0.8,
        summarizer=summarizer,
    )
    entries = [_user("short")]
    out = await policy.compact(
        _req(
            entries,
            provider=_FakeProviderWithWindow(),
            model="fake-model",
            last_prompt_tokens=100,
        )
    )
    assert out.entries is entries
    assert out.changed is False
    assert summarizer.calls == []


async def test_compacting_compacts_when_over_threshold():
    summarizer = _FakeSummarizer("Goal: ship feature.")
    policy = CompactingContextPolicy(
        window_tokens=1_000,
        trigger_ratio=0.5,  # threshold = 500
        keep_recent=2,
        summarizer=summarizer,
    )
    entries = [_user(f"m{i}") for i in range(10)]
    out = await policy.compact(
        _req(
            entries,
            provider=_FakeProviderWithWindow(window=1_000),
            model="fake-model",
            last_prompt_tokens=900,  # over threshold
            session_id="sess-1",
            run_id="run-1",
        )
    )
    assert out.changed is True
    assert out.reason == "auto_summary"
    assert out.summary == "Goal: ship feature."
    head = out.entries[0]
    assert isinstance(head, InputEntry)
    assert "Goal: ship feature." in head.content
    assert out.entries[1:] == entries[-2:]
    # The input list is never mutated.
    assert entries == [_user(f"m{i}") for i in range(10)]


async def test_compacting_falls_back_to_provider_context_window():
    summarizer = _FakeSummarizer()
    policy = CompactingContextPolicy(
        window_tokens=None,  # let provider answer
        trigger_ratio=0.5,
        keep_recent=2,
        summarizer=summarizer,
    )
    entries = [_user("x" * 100) for _ in range(10)]
    out = await policy.compact(
        _req(
            entries,
            provider=_FakeProviderWithWindow(window=1_000),
            model="fake-model",
            last_prompt_tokens=600,
        )
    )
    assert out.changed is True
    assert summarizer.calls  # summarizer was invoked


async def test_compacting_skips_when_no_window_info_available():
    summarizer = _FakeSummarizer()
    policy = CompactingContextPolicy(window_tokens=None, summarizer=summarizer)
    entries = [_user("x") for _ in range(10)]
    out = await policy.compact(
        _req(
            entries,
            provider=_FakeProviderWithWindow(window=None),
            model="unknown-model",
            last_prompt_tokens=999_999,
        )
    )
    # Without window info, proactive compaction is disabled.
    assert out.entries is entries
    assert out.changed is False
    assert summarizer.calls == []


async def test_compacting_reactive_always_compacts():
    summarizer = _FakeSummarizer("Reactive summary.")
    policy = CompactingContextPolicy(window_tokens=None, summarizer=summarizer)
    entries = [_user(f"m{i}") for i in range(15)]
    out = await policy.compact(_req(entries, provider=None, model=None, overflow=True))
    assert out.changed is True
    assert out.reason == "reactive_summary"
    assert isinstance(out.entries[0], InputEntry)
    assert "Reactive summary." in out.entries[0].content
    # Reactive keeps a small recent tail.
    assert len(out.entries) <= 9


# ---------------------------------------------------------------------------
# Stale tool-result trimming (structural move)
# ---------------------------------------------------------------------------


async def test_stale_tool_results_replaced_with_marker():
    policy = CompactingContextPolicy(
        window_tokens=10_000_000, summarizer=_FakeSummarizer()
    )
    entries = [
        _call("c1"),
        _out("c1", "first-result " * 50),
        _call("c2"),
        _out("c2", "second-result " * 50),
        _call("c3"),
        _out("c3", "third-result " * 50),
        _call("c4"),
        _out("c4", "fourth-result " * 50),
    ]
    out = await policy.compact(
        _req(entries, provider=_FakeProviderWithWindow(), model="fake-model")
    )
    assert out.changed is True
    assert out.reason == "context_structural"
    # Oldest result replaced with a recall marker; recent three kept intact.
    assert "recall_tool_result" in out.entries[1].output
    assert "c1" in out.entries[1].output
    assert "second-result" in out.entries[3].output
    assert "fourth-result" in out.entries[7].output


async def test_short_tool_results_not_trimmed():
    policy = CompactingContextPolicy(
        window_tokens=10_000_000, summarizer=_FakeSummarizer()
    )
    entries = [
        _call("c1"),
        _out("c1", "tiny"),
        _call("c2"),
        _out("c2", "tiny"),
        _call("c3"),
        _out("c3", "tiny"),
        _call("c4"),
        _out("c4", "tiny"),
    ]
    out = await policy.compact(
        _req(entries, provider=_FakeProviderWithWindow(), model="fake-model")
    )
    # Short outputs stay inline even when older than keep_recent tool results.
    assert out.changed is False
    assert out.entries is entries


# ---------------------------------------------------------------------------
# Incremental running summary (A+)
# ---------------------------------------------------------------------------


async def test_running_summary_folds_new_span_incrementally():
    summarizer = _FakeSummarizer("S1")
    policy = CompactingContextPolicy(
        window_tokens=1_000,
        trigger_ratio=0.5,
        keep_recent=2,
        summarizer=summarizer,
    )
    scratch: dict = {}
    ctx_kw = dict(
        provider=_FakeProviderWithWindow(window=1_000),
        model="fake-model",
        last_prompt_tokens=900,
        scratch=scratch,
    )
    entries = [_user(f"m{i}") for i in range(10)]
    out1 = await policy.compact(_req(entries, **ctx_kw))
    assert out1.changed is True
    assert summarizer.priors[-1] is None  # first summary from scratch
    assert scratch["_ctx_summary_covered"] == 8

    # Two more entries appended; only the new span should be folded in.
    summarizer.text = "S2"
    entries2 = entries + [_user("m10"), _user("m11")]
    out2 = await policy.compact(_req(entries2, **ctx_kw))
    assert out2.changed is True
    assert summarizer.priors[-1] == "S1"  # folded prior summary
    assert summarizer.calls[-1] == [_user("m8"), _user("m9")]  # only new span
    assert scratch["_ctx_summary_covered"] == 10


async def test_scratch_isolated_so_no_cross_run_leak():
    """A fresh scratch dict means a new run never sees another run's summary."""
    summarizer = _FakeSummarizer("S")
    policy = CompactingContextPolicy(
        window_tokens=1_000, trigger_ratio=0.5, keep_recent=2, summarizer=summarizer
    )
    entries = [_user(f"m{i}") for i in range(10)]
    base = dict(
        provider=_FakeProviderWithWindow(window=1_000),
        model="fake-model",
        last_prompt_tokens=900,
    )
    await policy.compact(_req(entries, scratch={}, **base))
    # New run, new scratch → prior_summary is None again.
    await policy.compact(_req(entries, scratch={}, **base))
    assert summarizer.priors == [None, None]


# ---------------------------------------------------------------------------
# Validation & circuit breaker
# ---------------------------------------------------------------------------


def test_compacting_policy_validates_parameters():
    with pytest.raises(ValueError, match="trigger_ratio"):
        CompactingContextPolicy(trigger_ratio=1)
    with pytest.raises(ValueError, match="keep_recent"):
        CompactingContextPolicy(keep_recent=0)


async def test_compacting_circuit_breaker():
    failing = _FailingSummarizer()
    policy = CompactingContextPolicy(
        window_tokens=100,
        trigger_ratio=0.5,
        keep_recent=1,
        summarizer=failing,
    )
    entries = [_user("x" * 1000) for _ in range(4)]
    base = dict(provider=None, model=None, last_prompt_tokens=500, scratch={})
    # Proactive failures don't crash; after the limit the breaker stops trying.
    for _ in range(5):
        out = await policy.compact(_req(entries, **base))
        assert out.changed is False
    assert failing.calls == 3  # _SUMMARY_FAILURE_LIMIT


async def test_reactive_summarizer_failure_propagates():
    failing = _FailingSummarizer()
    policy = CompactingContextPolicy(summarizer=failing)
    entries = [_user("x" * 1000) for _ in range(4)]
    with pytest.raises(RuntimeError, match="boom"):
        await policy.compact(
            _req(entries, provider=None, model=None, overflow=True, scratch={})
        )


async def test_compacting_uses_current_entries_when_last_prompt_is_stale():
    summarizer = _FakeSummarizer("compacted")
    policy = CompactingContextPolicy(
        window_tokens=1_000,
        trigger_ratio=0.5,  # threshold = 500
        keep_recent=2,
        summarizer=summarizer,
    )
    entries = [_user("x" * 400) for _ in range(10)]
    out = await policy.compact(
        _req(
            entries,
            provider=_FakeProviderWithWindow(window=1_000),
            model="fake-model",
            last_prompt_tokens=100,  # stale: from a much earlier turn
        )
    )
    assert out.changed is True
    assert summarizer.calls


# ---------------------------------------------------------------------------
# recall_tool_result tool
# ---------------------------------------------------------------------------


async def test_recall_tool_result_returns_full_output():
    entries = [
        _call("c1"),
        _out("c1", "the full output"),
        _user("hi"),
    ]
    agent = Agent(name="t", instructions="x", model=_FakeProviderWithWindow())
    ctx = RunContext(context=None, entries=entries, agent=agent)
    got = await run_tool(recall_tool_result, {"call_id": "c1"}, ctx)
    assert got == "the full output"


async def test_recall_tool_result_missing_call_id():
    agent = Agent(name="t", instructions="x", model=_FakeProviderWithWindow())
    ctx = RunContext(context=None, entries=[_user("hi")], agent=agent)
    got = await run_tool(recall_tool_result, {"call_id": "nope"}, ctx)
    assert "No tool result found" in got


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
        from lovia.transcript import FinishDelta, TextDelta, UsageDelta
        from lovia.messages import Usage

        yield TextDelta(text="hello after compaction")
        yield UsageDelta(usage=Usage(input_tokens=10, output_tokens=2))
        yield FinishDelta(reason="stop")


async def test_runner_reactive_compaction_recovers_from_overflow():
    summarizer = _FakeSummarizer("Compacted history.")
    policy = CompactingContextPolicy(window_tokens=None, summarizer=summarizer)
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="be brief", model=provider)
    result = await Runner.run(agent, "hello there", context_policy=policy)
    assert provider.stream_count == 2
    assert len(summarizer.calls) == 1
    assert "hello after compaction" in (result.output or "")


async def test_runner_emits_context_compacted_event():
    summarizer = _FakeSummarizer("S.")
    policy = CompactingContextPolicy(window_tokens=None, summarizer=summarizer)
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    events_seen: list = []
    async for ev in Runner.stream(agent, "go", context_policy=policy):
        events_seen.append(ev)
    compacted = [e for e in events_seen if isinstance(e, ContextCompacted)]
    assert len(compacted) == 1
    assert compacted[0].reactive is True
    assert compacted[0].reason == "reactive_summary"
    assert compacted[0].summary == "S."


async def test_runner_no_policy_keeps_existing_behavior():
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", instructions="x", model=provider)
    result = await Runner.run(agent, "ping")
    assert result.output == "hi"


async def test_runner_default_policy_recovers_from_overflow():
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    events_seen: list = []
    async for ev in Runner.stream(agent, "go"):
        events_seen.append(ev)
    compacted = [e for e in events_seen if isinstance(e, ContextCompacted)]
    assert len(compacted) == 1
    assert compacted[0].reactive is True
    assert "hello after compaction" in (events_seen[-1].result.output or "")


async def test_compaction_does_not_modify_session():
    """View-only: the Session stores the full transcript, never the compacted view."""
    summarizer = _FakeSummarizer("S.")
    policy = CompactingContextPolicy(window_tokens=None, summarizer=summarizer)
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    sess = InMemorySession()
    await Runner.run(
        agent, "first", context_policy=policy, session=sess, session_id="s1"
    )
    persisted = await sess.load("s1")
    # The summary marker must NOT be persisted (it only lived in the view).
    assert not any(
        isinstance(it, InputEntry) and "[Reactive context summary]" in str(it.content)
        for it in persisted
    )
    # The real user input and assistant reply are persisted in full.
    assert any(isinstance(it, InputEntry) and it.content == "first" for it in persisted)
    assert any(
        isinstance(it, AssistantTextEntry) and "hello after compaction" in it.content
        for it in persisted
    )
