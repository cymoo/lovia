"""Tests for the ContextPolicy stack."""

from __future__ import annotations

import pytest

from lovia import (
    Agent,
    ArchiveRef,
    CompactingContextPolicy,
    ContextOverflowError,
    FileCompactionArchive,
    InputEntry,
    AssistantTextEntry,
    NoopContextPolicy,
    Runner,
    ToolCallEntry,
    ToolResultEntry,
    safe_window,
)
from lovia.context import PolicyContext
from lovia.events import ContextCompacted
from lovia.stores.memory import InMemorySession

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
    call_ids_in_result = {
        it.call_id for it in got if isinstance(it, ToolCallEntry)
    }
    for it in got:
        if isinstance(it, ToolResultEntry):
            assert it.call_id in call_ids_in_result, (
                f"Orphan ToolResultEntry {it.call_id} in result"
            )


# ---------------------------------------------------------------------------
# NoopContextPolicy
# ---------------------------------------------------------------------------


async def test_noop_policy_returns_same_list_object():
    policy = NoopContextPolicy()
    entries = [_user("hi")]
    out = await policy.apply(entries, ctx=PolicyContext(provider=None, model=None))
    assert out.entries is entries
    assert out.changed is False
    out2 = await policy.apply_reactive(
        entries, ctx=PolicyContext(provider=None, model=None)
    )
    assert out2.entries is entries
    assert out2.changed is False


# ---------------------------------------------------------------------------
# CompactingContextPolicy: unit-level
# ---------------------------------------------------------------------------


class _FakeSummarizer:
    def __init__(self, text: str = "SUMMARY_TEXT") -> None:
        self.text = text
        self.calls: list[list] = []

    async def summarize(self, entries, *, ctx):
        self.calls.append(list(entries))
        return self.text


class _FailingSummarizer:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, entries, *, ctx):
        self.calls += 1
        raise RuntimeError("boom")


class _FailingArchive:
    async def save_transcript(self, entries, *, ctx, reason):
        raise RuntimeError("archive down")

    async def save_tool_result(self, output, *, call_id, ctx):
        raise RuntimeError("archive down")


class _RecordingArchive:
    def __init__(self) -> None:
        self.transcripts: list[list] = []
        self.tool_results: list[tuple[str, str]] = []

    async def save_transcript(self, entries, *, ctx, reason):
        self.transcripts.append(list(entries))
        return ArchiveRef(uri=f"memory://transcript/{len(self.transcripts)}", kind="transcript")

    async def save_tool_result(self, output, *, call_id, ctx):
        self.tool_results.append((call_id, output))
        return ArchiveRef(uri=f"memory://tool/{call_id}", kind="tool_result")


class _FakeProviderWithWindow:
    """A stand-in provider that just answers context_window queries."""

    name = "fake"

    def __init__(self, *, window: int | None = 1000) -> None:
        self.model = "fake-model"
        self._window = window

    def context_window(self, model: str) -> int | None:
        return self._window


async def test_compacting_skips_when_under_threshold():
    summarizer = _FakeSummarizer()
    policy = CompactingContextPolicy(
        context_window_tokens=10_000,
        trigger_ratio=0.8,
        max_entries=None,
        keep_recent_tool_results=None,
        tool_result_budget_chars=None,
        summarizer=summarizer,
    )
    entries = [_user("short")]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(),
        model="fake-model",
        last_input_tokens=100,
    )
    out = await policy.apply(entries, ctx=ctx)
    assert out.entries is entries
    assert out.changed is False
    assert summarizer.calls == []


async def test_compacting_compacts_when_over_threshold(tmp_path):
    summarizer = _FakeSummarizer("Goal: ship feature.")
    archive = FileCompactionArchive(root=tmp_path)
    policy = CompactingContextPolicy(
        context_window_tokens=1_000,
        trigger_ratio=0.5,  # threshold = 500
        max_entries=None,
        keep_recent_entries=2,
        keep_recent_tool_results=None,
        tool_result_budget_chars=None,
        summarizer=summarizer,
        archive=archive,
    )
    entries = [_user(f"m{i}") for i in range(10)]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(window=1_000),
        model="fake-model",
        last_input_tokens=900,  # over threshold
        session_id="sess-1",
        run_id="run-1",
    )
    out = await policy.apply(entries, ctx=ctx)
    assert out.changed is True
    assert out.reason == "auto_summary"
    assert out.summary == "Goal: ship feature."
    head = out.entries[0]
    assert isinstance(head, InputEntry)
    assert "Goal: ship feature." in head.content
    assert out.entries[1:] == entries[-2:]
    assert out.archive_ref is not None
    assert out.archive_ref.kind == "transcript"
    assert "sess-1" in out.archive_ref.uri
    assert "run-1" in out.archive_ref.uri


async def test_compacting_falls_back_to_provider_context_window():
    summarizer = _FakeSummarizer()
    policy = CompactingContextPolicy(
        context_window_tokens=None,  # let provider answer
        trigger_ratio=0.5,
        max_entries=None,
        keep_recent_tool_results=None,
        tool_result_budget_chars=None,
        summarizer=summarizer,
    )
    entries = [_user("x" * 100) for _ in range(10)]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(window=1_000),
        model="fake-model",
        last_input_tokens=600,
    )
    out = await policy.apply(entries, ctx=ctx)
    assert out.changed is True
    assert summarizer.calls  # summarizer was invoked


async def test_compacting_skips_when_no_window_info_available():
    summarizer = _FakeSummarizer()
    policy = CompactingContextPolicy(
        context_window_tokens=None,
        max_entries=None,
        keep_recent_tool_results=None,
        tool_result_budget_chars=None,
        summarizer=summarizer,
    )
    entries = [_user("x") for _ in range(10)]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(window=None),
        model="unknown-model",
        last_input_tokens=999_999,
    )
    out = await policy.apply(entries, ctx=ctx)
    # Without window info, proactive compaction is disabled.
    assert out.entries is entries
    assert out.changed is False
    assert summarizer.calls == []


async def test_compacting_reactive_always_compacts():
    summarizer = _FakeSummarizer("Reactive summary.")
    policy = CompactingContextPolicy(
        context_window_tokens=None,
        summarizer=summarizer,
        reactive_keep_recent_entries=1,
    )
    entries = [_user(f"m{i}") for i in range(5)]
    ctx = PolicyContext(provider=None, model=None)
    out = await policy.apply_reactive(entries, ctx=ctx)
    assert out.changed is True
    assert out.reason == "reactive_summary"
    assert len(out.entries) == 2  # summary + 1 tail
    assert isinstance(out.entries[0], InputEntry)
    assert "Reactive summary." in out.entries[0].content


async def test_compacting_tool_result_retention_replaces_old_outputs():
    summarizer = _FakeSummarizer()
    policy = CompactingContextPolicy(
        context_window_tokens=10_000_000,  # never proactively summarize
        max_entries=None,
        keep_recent_tool_results=1,
        tool_result_budget_chars=None,
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
    assert out.changed is True
    assert out.reason == "context_stages"
    # First two outputs replaced, last one preserved.
    assert "compacted" in out.entries[1].output.lower()
    assert "compacted" in out.entries[3].output.lower()
    assert "third-result" in out.entries[5].output


async def test_tool_result_retention_can_keep_zero_results():
    policy = CompactingContextPolicy(
        context_window_tokens=10_000_000,
        max_entries=None,
        keep_recent_tool_results=0,
        tool_result_budget_chars=None,
        summarizer=_FakeSummarizer(),
    )
    entries = [
        _call("c1"),
        _out("c1", "first-result " * 50),
        _call("c2"),
        _out("c2", "second-result " * 50),
    ]
    out = await policy.apply(
        entries,
        ctx=PolicyContext(provider=_FakeProviderWithWindow(), model="fake-model"),
    )
    assert out.changed is True
    assert "compacted" in out.entries[1].output.lower()
    assert "compacted" in out.entries[3].output.lower()


async def test_tool_result_budget_persists_large_outputs(tmp_path):
    policy = CompactingContextPolicy(
        context_window_tokens=10_000_000,
        max_entries=None,
        keep_recent_tool_results=None,
        tool_result_budget_chars=500,
        large_tool_result_chars=100,
        tool_result_preview_chars=30,
        archive=FileCompactionArchive(root=tmp_path),
        summarizer=_FakeSummarizer(),
    )
    entries = [_call("c1"), _out("c1", "x" * 2_000)]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(),
        model="fake-model",
        session_id="s1",
    )
    out = await policy.apply(entries, ctx=ctx)
    assert out.changed is True
    assert out.reason == "context_stages"
    result = out.entries[1]
    assert isinstance(result, ToolResultEntry)
    assert result.output.startswith("[Persisted tool result]")
    archived = list((tmp_path / "tool-results" / "s1").glob("*.txt"))
    assert len(archived) == 1
    assert archived[0].read_text() == "x" * 2_000


async def test_file_archive_accepts_dynamic_root(tmp_path):
    archive = FileCompactionArchive(
        root=lambda ctx: tmp_path / "projects" / (ctx.session_id or "default")
    )
    ctx = PolicyContext(provider=None, model=None, session_id="s1", run_id="r1")
    ref = await archive.save_transcript([_user("hello")], ctx=ctx, reason="test")
    assert ref.kind == "transcript"
    path = tmp_path / "projects" / "s1" / "transcripts" / "s1"
    assert list(path.glob("r1-*.jsonl"))


async def test_tool_result_budget_falls_back_to_preview_when_archive_fails():
    policy = CompactingContextPolicy(
        context_window_tokens=10_000_000,
        max_entries=None,
        keep_recent_tool_results=None,
        tool_result_budget_chars=500,
        large_tool_result_chars=100,
        tool_result_preview_chars=12,
        archive=_FailingArchive(),
        summarizer=_FakeSummarizer(),
    )
    entries = [_call("c1"), _out("c1", "abcdef" * 200)]
    out = await policy.apply(
        entries,
        ctx=PolicyContext(provider=_FakeProviderWithWindow(), model="fake-model"),
    )
    assert out.changed is True
    result = out.entries[1]
    assert isinstance(result, ToolResultEntry)
    assert result.output.startswith("[Tool result preview]")
    assert "abcdefabcdef" in result.output


async def test_middle_snip_trims_long_transcripts():
    policy = CompactingContextPolicy(
        context_window_tokens=10_000_000,
        max_entries=6,
        keep_initial_entries=2,
        keep_recent_entries=2,
        keep_recent_tool_results=None,
        tool_result_budget_chars=None,
        summarizer=_FakeSummarizer(),
    )
    entries = [_user(f"m{i}") for i in range(10)]
    out = await policy.apply(
        entries,
        ctx=PolicyContext(provider=_FakeProviderWithWindow(), model="fake-model"),
    )
    assert out.changed is True
    assert out.reason == "context_stages"
    assert [it.content for it in out.entries] == [
        "m0",
        "m1",
        "[Snipped 6 earlier transcript entries.]",
        "m8",
        "m9",
    ]


async def test_summary_archives_original_transcript_before_stage_rewrites():
    archive = _RecordingArchive()
    summarizer = _FakeSummarizer("summary")
    policy = CompactingContextPolicy(
        context_window_tokens=100,
        trigger_ratio=0.5,
        max_entries=None,
        keep_recent_entries=1,
        keep_recent_tool_results=0,
        tool_result_budget_chars=None,
        archive=archive,
        summarizer=summarizer,
    )
    entries = [_call("c1"), _out("c1", "full-result " * 100)]
    out = await policy.apply(
        entries,
        ctx=PolicyContext(
            provider=_FakeProviderWithWindow(),
            model="fake-model",
            last_input_tokens=1_000,
        ),
    )
    assert out.changed is True
    assert out.reason == "auto_summary"
    assert archive.transcripts
    archived_result = archive.transcripts[0][1]
    assert isinstance(archived_result, ToolResultEntry)
    assert "full-result" in archived_result.output
    summarized_result = summarizer.calls[0][1]
    assert isinstance(summarized_result, ToolResultEntry)
    assert "compacted" in summarized_result.output.lower()


def test_compacting_policy_validates_parameters():
    with pytest.raises(ValueError, match="trigger_ratio"):
        CompactingContextPolicy(trigger_ratio=1)
    with pytest.raises(ValueError, match="keep_recent_entries"):
        CompactingContextPolicy(keep_recent_entries=0)
    with pytest.raises(ValueError, match="reactive_keep_recent_entries"):
        CompactingContextPolicy(reactive_keep_recent_entries=0)
    with pytest.raises(ValueError, match="keep_recent_tool_results"):
        CompactingContextPolicy(keep_recent_tool_results=-1)


async def test_compacting_circuit_breaker():
    failing = _FailingSummarizer()
    policy = CompactingContextPolicy(
        context_window_tokens=100,
        trigger_ratio=0.5,
        max_entries=None,
        keep_recent_tool_results=None,
        tool_result_budget_chars=None,
        summarizer=failing,
        max_summary_failures=2,
    )
    entries = [_user("x" * 1000)]
    ctx = PolicyContext(
        provider=None,
        model=None,
        last_input_tokens=500,
    )
    # Proactive summary failures do not crash a run; after two failures the
    # circuit breaker stops trying.
    out = await policy.apply(entries, ctx=ctx)
    assert out.changed is False
    out = await policy.apply(entries, ctx=ctx)
    assert out.changed is False
    out = await policy.apply(entries, ctx=ctx)
    assert out.changed is False
    assert failing.calls == 2


async def test_reactive_summarizer_failure_propagates():
    failing = _FailingSummarizer()
    policy = CompactingContextPolicy(summarizer=failing, max_summary_failures=2)
    entries = [_user("x" * 1000)]
    ctx = PolicyContext(provider=None, model=None)
    with pytest.raises(RuntimeError, match="boom"):
        await policy.apply_reactive(entries, ctx=ctx)


async def test_compacting_uses_current_entries_when_last_prompt_is_stale():
    """Regression: ``last_input_tokens`` is the *previous* turn's prompt
    size — it does not include the assistant reply, tool results, or new
    user message that have been appended since. The policy must therefore
    fall back to the current entries estimate when it is larger; otherwise a
    big tool result silently overshoots the model's hard cap before the
    next ``usage`` count arrives.
    """
    summarizer = _FakeSummarizer("compacted")
    policy = CompactingContextPolicy(
        context_window_tokens=1_000,
        trigger_ratio=0.5,  # threshold = 500
        max_entries=None,
        keep_recent_entries=2,
        keep_recent_tool_results=None,
        tool_result_budget_chars=None,
        summarizer=summarizer,
    )
    # 10 messages of ~400 chars each ≈ 1000 estimated tokens, well above
    # threshold. ``last_input_tokens`` is stale and below threshold.
    entries = [_user("x" * 400) for _ in range(10)]
    ctx = PolicyContext(
        provider=_FakeProviderWithWindow(window=1_000),
        model="fake-model",
        last_input_tokens=100,  # stale: from a much earlier turn
    )
    out = await policy.apply(entries, ctx=ctx)
    assert out.changed is True, (
        "expected compaction to trigger from current-entries estimate "
        "despite stale last_input_tokens"
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
    policy = CompactingContextPolicy(
        context_window_tokens=None,
        summarizer=summarizer,
        reactive_keep_recent_entries=1,
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
    policy = CompactingContextPolicy(
        context_window_tokens=None,
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
    assert compacted[0].reason == "reactive_summary"
    assert compacted[0].summary == "S."


async def test_runner_no_policy_keeps_existing_behavior():
    """Sanity: the default policy doesn't alter normal short runs."""
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
    assert compacted[0].reason == "reactive_summary"
    completed = [e for e in events_seen if getattr(e, "result", None) is not None]
    assert completed
    assert "hello after compaction" in (completed[-1].result.output or "")


async def test_runner_session_replace_after_compaction():
    summarizer = _FakeSummarizer("S.")
    policy = CompactingContextPolicy(
        context_window_tokens=None,
        summarizer=summarizer,
        reactive_keep_recent_entries=1,
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
    assert any(
        isinstance(it, InputEntry) and "S." in str(it.content)
        for it in persisted
    )
    # The final assistant reply must also be persisted.
    assert any(
        isinstance(it, AssistantTextEntry) and "hello after compaction" in it.content
        for it in persisted
    )
