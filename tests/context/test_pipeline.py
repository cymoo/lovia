"""Tests for Compaction: gating, hysteresis, stickiness, reactive path,
and runner integration."""

from __future__ import annotations

import json

import pytest

from lovia import (
    Agent,
    AssistantTextEntry,
    ContextOverflowError,
    Compaction,
    InMemorySession,
    InputEntry,
    NoopContextPolicy,
    Runner,
)
from lovia.context import CompactionState
from lovia.events import ContextCompacted
from lovia.transcript import ToolCallEntry, ToolResultEntry, entry_to_dict

from ..scripted_provider import ScriptedProvider, text
from .helpers import (
    FailingSummarizer,
    FakeProviderWithWindow,
    FakeSummarizer,
    FakeWorkspace,
    call,
    out,
    req,
    user,
)


def _assistant(s: str) -> AssistantTextEntry:
    return AssistantTextEntry(content=s)


def _pipeline(**kw) -> Compaction:
    kw.setdefault("reserve_output_tokens", 0)
    return Compaction(**kw)


# ---------------------------------------------------------------------------
# Construction & gating
# ---------------------------------------------------------------------------


def test_pipeline_validates_parameters():
    with pytest.raises(ValueError, match="compact_at"):
        Compaction(compact_at=1.5)
    with pytest.raises(ValueError, match="compact_to"):
        Compaction(compact_at=0.5, compact_to=0.5)
    with pytest.raises(ValueError, match="context_window"):
        Compaction(context_window=0)
    with pytest.raises(ValueError, match="keep_recent_tokens"):
        Compaction(keep_recent_tokens=0)
    with pytest.raises(ValueError, match="reserve_output_tokens"):
        Compaction(reserve_output_tokens=-1)


async def test_noop_policy_returns_same_list_object():
    entries = [user("hi")]
    res = await NoopContextPolicy().compact(req(entries))
    assert res.entries is entries
    assert res.changed is False and res.compacted is False


async def test_under_trigger_no_compaction_and_no_quality_loss():
    """Below the trigger nothing is touched — even old tool results."""
    summarizer = FakeSummarizer()
    pipeline = _pipeline(context_window=1_000_000, summarizer=summarizer)
    entries = [user("go")]
    for i in range(8):
        entries += [call(f"c{i}"), out(f"c{i}", "r" * 2_000)]
    res = await pipeline.compact(req(entries))
    assert res.compacted is False
    assert res.changed is False
    assert all(a is b for a, b in zip(res.entries, entries))
    assert summarizer.calls == []


async def test_no_window_info_disables_proactive_compaction():
    summarizer = FakeSummarizer()
    pipeline = Compaction(summarizer=summarizer)  # no window anywhere
    entries = [user("x" * 4_000) for _ in range(50)]
    res = await pipeline.compact(
        req(
            entries,
            provider=FakeProviderWithWindow(window=None),
            model="m",
            last_input_tokens=999_999,
        )
    )
    assert res.compacted is False
    assert summarizer.calls == []


async def test_window_falls_back_to_provider():
    summarizer = FakeSummarizer()
    pipeline = Compaction(summarizer=summarizer)  # default reserve
    entries = [user("x" * 100) for _ in range(30)]  # ~990 tokens
    res = await pipeline.compact(
        req(entries, provider=FakeProviderWithWindow(window=1_000), model="fake-model")
    )
    # usable = 500 (reserve >= window fallback), trigger 375 < 990.
    assert res.compacted is True
    assert summarizer.calls


# ---------------------------------------------------------------------------
# Proactive summary burst
# ---------------------------------------------------------------------------


async def test_over_trigger_summarizes_prefix_and_keeps_token_tail():
    summarizer = FakeSummarizer("Goal: ship feature.")
    pipeline = _pipeline(
        context_window=1_000, compact_at=0.5, compact_to=0.3, summarizer=summarizer
    )
    entries = [user(f"m{i}" + "x" * 98) for i in range(30)]
    res = await pipeline.compact(req(entries, session_id="s", run_id="r"))

    assert res.compacted is True
    assert res.reason == "summary"
    assert res.summary == "Goal: ship feature."
    head = res.entries[0]
    assert isinstance(head, InputEntry) and "Goal: ship feature." in head.content
    # Tail kept verbatim, sized by tokens (usable//5 = 200 → 6 entries).
    assert res.entries[1:] == entries[-6:]
    assert res.tokens_before is not None and res.tokens_after is not None
    assert res.tokens_after < res.tokens_before
    # The input list was never mutated.
    assert len(entries) == 30 and all(isinstance(e, InputEntry) for e in entries)


async def test_giant_old_entry_compacts_but_many_small_do_not():
    # Budgets are token-based, not entry-count-based.
    summarizer = FakeSummarizer()
    pipeline = _pipeline(context_window=8_000, summarizer=summarizer)
    giant_first = [user("x" * 40_000), _assistant("noted"), user("recent question")]
    res = await pipeline.compact(req(giant_first))
    assert res.compacted is True and len(summarizer.calls) == 1

    many_small = [user("x" * 40) for _ in range(100)]  # ~1.8K tokens
    res2 = await pipeline.compact(req(many_small))
    assert res2.compacted is False


# ---------------------------------------------------------------------------
# Tool-result clearing burst
# ---------------------------------------------------------------------------


async def test_clearing_satisfies_budget_without_summary():
    summarizer = FakeSummarizer()
    pipeline = _pipeline(context_window=10_000, summarizer=summarizer)
    entries: list = [user("task")]
    for i in range(20):
        entries += [call(f"c{i}"), out(f"c{i}", "r" * 2_000)]
    res = await pipeline.compact(req(entries))

    assert res.compacted is True
    assert res.reason == "clear"
    assert summarizer.calls == []  # cheap stage was enough
    cleared = [
        e
        for e in res.entries
        if getattr(e, "output", "").startswith("[Earlier tool result cleared")
    ]
    assert cleared
    # The most recent results stay verbatim.
    assert res.entries[-1].output == "r" * 2_000
    assert res.tokens_after < res.tokens_before


# ---------------------------------------------------------------------------
# Stickiness, hysteresis, cache stability
# ---------------------------------------------------------------------------


async def test_sticky_replay_after_burst():
    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000, compact_at=0.5, compact_to=0.3, summarizer=summarizer
    )
    scratch: dict = {}
    entries = [user("x" * 100) for _ in range(30)]
    first = await pipeline.compact(req(entries, scratch=scratch))
    assert first.compacted is True

    second = await pipeline.compact(req(entries, scratch=scratch))
    assert second.compacted is False
    assert second.changed is True
    assert second.reason == "sticky_replay"
    assert len(summarizer.calls) == 1  # no new LLM work
    assert [entry_to_dict(e) for e in second.entries] == [
        entry_to_dict(e) for e in first.entries
    ]


async def test_decisions_are_monotonic_and_prefix_stable_across_growth():
    """The headline cache-stability property: decisions only accumulate, and a
    non-compacting turn's view extends the previous turn's view verbatim."""
    summarizer = FakeSummarizer()
    pipeline = _pipeline(context_window=6_000, summarizer=summarizer)
    scratch: dict = {}
    entries: list = [user("the task")]

    prev_view_dicts: list | None = None
    prev_cleared: set = set()
    prev_covered = 0
    for i in range(12):
        entries += [call(f"c{i}"), out(f"c{i}", "r" * 1_200)]
        res = await pipeline.compact(req(entries, scratch=scratch))
        state = CompactionState.load(scratch)

        assert prev_cleared <= state.cleared  # never un-clears
        covered = state.summary.covered if state.summary else 0
        assert covered >= prev_covered  # coverage only grows
        view_dicts = [entry_to_dict(e) for e in res.entries]
        if prev_view_dicts is not None and not res.compacted:
            # Sticky replay: previous view is a verbatim prefix of this one.
            assert view_dicts[: len(prev_view_dicts)] == prev_view_dicts

        prev_view_dicts = view_dicts
        prev_cleared = set(state.cleared)
        prev_covered = covered

    # Compaction fired in bursts, not on every one of the 12 turns.
    assert len(summarizer.calls) <= 3


async def test_offload_then_summary_through_pipeline():
    summarizer = FakeSummarizer("S")
    workspace = FakeWorkspace()
    pipeline = _pipeline(
        context_window=4_000, compact_at=0.5, compact_to=0.25, summarizer=summarizer
    )
    entries: list = [user("task")]
    for i in range(4):
        entries += [call(f"c{i}"), out(f"c{i}", "A" * 6_000)]
    res = await pipeline.compact(req(entries, workspace=workspace))

    assert res.compacted is True
    assert "offload" in res.reason
    assert workspace.files  # something was archived
    for path, content in workspace.files.items():
        assert path.startswith(".context/tool-")
        assert content == "A" * 6_000


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


async def test_ratio_calibrates_against_real_usage():
    pipeline = _pipeline(context_window=1_000_000)
    scratch: dict = {}
    entries = [user("x" * 400) for _ in range(10)]
    await pipeline.compact(req(entries, scratch=scratch))
    estimate = CompactionState.load(scratch).last_view_estimate
    assert estimate is not None

    await pipeline.compact(
        req(entries, scratch=scratch, last_input_tokens=2 * estimate)
    )
    state = CompactionState.load(scratch)
    assert state.ratio == pytest.approx(0.8 * 1.0 + 0.2 * 2.0)


# ---------------------------------------------------------------------------
# Fingerprint reset (handoff / input_filter rewrote history)
# ---------------------------------------------------------------------------


async def test_rewritten_prefix_resets_summary_but_keeps_clear_decisions():
    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000, compact_at=0.5, compact_to=0.3, summarizer=summarizer
    )
    scratch: dict = {}
    entries = [user("x" * 100) for _ in range(30)]
    await pipeline.compact(req(entries, scratch=scratch))
    assert CompactionState.load(scratch).summary is not None

    rewritten = [user("REWRITTEN HISTORY " + "y" * 100), *entries[1:]]
    res = await pipeline.compact(req(rewritten, scratch=scratch))
    # The stale summary was dropped and rebuilt from scratch (prior=None).
    assert summarizer.priors == [None, None]
    assert res.compacted is True


# ---------------------------------------------------------------------------
# Reactive overflow path
# ---------------------------------------------------------------------------


async def test_reactive_compacts_even_without_window_info():
    summarizer = FakeSummarizer("Reactive summary.")
    pipeline = Compaction(summarizer=summarizer)
    entries = [user(f"m{i}") for i in range(15)]
    res = await pipeline.compact(req(entries, overflow=True))
    assert res.compacted is True
    assert res.reason == "reactive_summary"
    assert isinstance(res.entries[0], InputEntry)
    assert "Reactive summary." in res.entries[0].content
    assert res.entries[-1] is entries[-1]  # most recent entry kept verbatim


async def test_reactive_ignores_refuted_window_claim():
    """A provider that claims a huge window but overflows anyway must still
    be compacted against the actual prompt size."""
    summarizer = FakeSummarizer()
    pipeline = Compaction(summarizer=summarizer)
    entries = [user("x" * 400) for _ in range(20)]
    res = await pipeline.compact(
        req(
            entries,
            provider=FakeProviderWithWindow(window=10_000_000),
            model="fake-model",
            overflow=True,
        )
    )
    assert res.compacted is True
    assert len(res.entries) < len(entries)


async def test_reactive_summarizer_failure_propagates_and_persists_counter():
    pipeline = Compaction(summarizer=FailingSummarizer())
    scratch: dict = {}
    entries = [user("x" * 1_000) for _ in range(4)]
    with pytest.raises(RuntimeError, match="boom"):
        await pipeline.compact(req(entries, overflow=True, scratch=scratch))
    assert CompactionState.load(scratch).summary_failures == 1


async def test_proactive_circuit_breaker_stops_after_limit():
    failing = FailingSummarizer()
    pipeline = _pipeline(
        context_window=100, compact_at=0.5, compact_to=0.25, summarizer=failing
    )
    entries = [user("x" * 1_000) for _ in range(4)]
    scratch: dict = {}
    for _ in range(5):
        res = await pipeline.compact(req(entries, scratch=scratch))
        assert res.compacted is False
    assert failing.calls == 3  # breaker tripped, stayed tripped


# ---------------------------------------------------------------------------
# Checkpoint / resume
# ---------------------------------------------------------------------------


async def test_state_survives_checkpoint_round_trip():
    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000, compact_at=0.5, compact_to=0.3, summarizer=summarizer
    )
    scratch: dict = {}
    entries = [user("x" * 100) for _ in range(30)]
    first = await pipeline.compact(req(entries, scratch=scratch))
    assert first.compacted is True

    revived_scratch = json.loads(json.dumps(scratch))
    res = await pipeline.compact(req(entries, scratch=revived_scratch))
    assert res.compacted is False  # decisions replayed, none re-made
    assert [entry_to_dict(e) for e in res.entries] == [
        entry_to_dict(e) for e in first.entries
    ]
    assert len(summarizer.calls) == 1


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
        return 10_000_000  # never trigger the proactive path

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


def _history(n_pairs: int = 10) -> list:
    seeded: list = []
    for i in range(n_pairs):
        seeded.append(user(f"question number {i} about pandas"))
        seeded.append(_assistant(f"answer number {i} about pandas"))
    return seeded


async def _seeded_session() -> InMemorySession:
    sess = InMemorySession()
    await sess.append("s1", _history())
    return sess


async def test_runner_reactive_compaction_recovers_from_overflow():
    summarizer = FakeSummarizer("Compacted history.")
    policy = Compaction(summarizer=summarizer)
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="be brief", model=provider)
    sess = await _seeded_session()
    result = await Runner.run(
        agent, "hello there", context_policy=policy, session=sess, session_id="s1"
    )
    assert provider.stream_count == 2
    assert len(summarizer.calls) == 1
    assert "hello after compaction" in (result.output or "")
    # The retried prompt was actually smaller.
    assert provider.last_input_lengths[1] < provider.last_input_lengths[0]


async def test_runner_emits_context_compacted_event():
    summarizer = FakeSummarizer("S.")
    policy = Compaction(summarizer=summarizer)
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    sess = await _seeded_session()
    events_seen: list = []
    async for ev in Runner.stream(
        agent, "go", context_policy=policy, session=sess, session_id="s1"
    ):
        events_seen.append(ev)
    compacted = [e for e in events_seen if isinstance(e, ContextCompacted)]
    assert len(compacted) == 1
    assert compacted[0].reactive is True
    assert compacted[0].reason == "reactive_summary"
    assert compacted[0].summary == "S."
    assert isinstance(compacted[0].metadata.get("tokens_after"), int)


async def test_runner_no_policy_keeps_existing_behavior():
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", instructions="x", model=provider)
    result = await Runner.run(agent, "ping")
    assert result.output == "hi"


async def test_runner_default_policy_recovers_from_overflow():
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    sess = await _seeded_session()
    events_seen: list = []
    async for ev in Runner.stream(agent, "go", session=sess, session_id="s1"):
        events_seen.append(ev)
    compacted = [e for e in events_seen if isinstance(e, ContextCompacted)]
    assert len(compacted) == 1
    assert compacted[0].reactive is True
    assert "hello after compaction" in (events_seen[-1].result.output or "")


async def test_runner_overflow_on_incompressible_prompt_propagates():
    """A 2-entry prompt has nothing to compact; the overflow must surface."""
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    with pytest.raises(ContextOverflowError):
        await Runner.run(agent, "hello")


async def test_compaction_does_not_modify_session():
    """View-only: the Session stores the full transcript, never the view."""
    summarizer = FakeSummarizer("S.")
    policy = Compaction(summarizer=summarizer)
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    sess = await _seeded_session()
    await Runner.run(
        agent, "first", context_policy=policy, session=sess, session_id="s1"
    )
    persisted = await sess.load("s1")
    assert not any(
        "<context_summary>" in str(getattr(e, "content", "")) for e in persisted
    )
    assert any(isinstance(e, InputEntry) and e.content == "first" for e in persisted)
    assert any(
        isinstance(e, AssistantTextEntry) and "hello after compaction" in e.content
        for e in persisted
    )


# ---------------------------------------------------------------------------
# Cross-run session state cache
# ---------------------------------------------------------------------------


async def test_session_cache_carries_decisions_across_runs():
    """A new run (fresh scratch) on the same session resumes prior decisions
    instead of re-summarizing the whole prefix."""
    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000, compact_at=0.5, compact_to=0.3, summarizer=summarizer
    )
    entries = [user("x" * 100) for _ in range(30)]
    first = await pipeline.compact(req(entries, scratch={}, session_id="chat-1"))
    assert first.compacted is True and len(summarizer.calls) == 1

    # Same session, brand-new run scratch, slightly grown history.
    grown = entries + [user("a follow-up question")]
    second = await pipeline.compact(req(grown, scratch={}, session_id="chat-1"))
    assert second.compacted is False  # sticky replay from the session cache
    assert second.reason == "sticky_replay"
    assert len(summarizer.calls) == 1  # no re-summarization


async def test_session_cache_isolated_between_sessions():
    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000, compact_at=0.5, compact_to=0.3, summarizer=summarizer
    )
    entries = [user("x" * 100) for _ in range(30)]
    await pipeline.compact(req(entries, scratch={}, session_id="chat-1"))
    await pipeline.compact(req(entries, scratch={}, session_id="chat-2"))
    # Different session: decisions re-derived (summarized from scratch).
    assert summarizer.priors == [None, None]


async def test_session_cache_resets_on_rewritten_history():
    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000, compact_at=0.5, compact_to=0.3, summarizer=summarizer
    )
    entries = [user("x" * 100) for _ in range(30)]
    await pipeline.compact(req(entries, scratch={}, session_id="chat-1"))
    different = [user("y" * 120) for _ in range(30)]
    res = await pipeline.compact(req(different, scratch={}, session_id="chat-1"))
    # Fingerprint mismatch → cached summary dropped, fresh one built.
    assert res.compacted is True
    assert summarizer.priors == [None, None]


async def test_session_cache_disabled_and_bounded():
    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000,
        compact_at=0.5,
        compact_to=0.3,
        summarizer=summarizer,
        session_state_cache=0,
    )
    entries = [user("x" * 100) for _ in range(30)]
    await pipeline.compact(req(entries, scratch={}, session_id="chat-1"))
    await pipeline.compact(req(entries, scratch={}, session_id="chat-1"))
    assert summarizer.priors == [None, None]  # cache off: re-derived

    bounded = _pipeline(
        context_window=1_000,
        compact_at=0.5,
        compact_to=0.3,
        summarizer=FakeSummarizer(),
        session_state_cache=2,
    )
    for sid in ("a", "b", "c"):
        await bounded.compact(req(entries, scratch={}, session_id=sid))
    assert len(bounded._session_states) == 2  # oldest evicted


async def test_anonymous_runs_never_touch_session_cache():
    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000, compact_at=0.5, compact_to=0.3, summarizer=summarizer
    )
    entries = [user("x" * 100) for _ in range(30)]
    await pipeline.compact(req(entries, scratch={}))  # no session_id
    assert pipeline._session_states == {}


# ---------------------------------------------------------------------------
# Aggressive recovery from one oversized tool result
# ---------------------------------------------------------------------------


async def test_reactive_recovers_when_latest_tool_result_is_the_problem():
    summarizer = FakeSummarizer()
    pipeline = Compaction(summarizer=summarizer)
    entries = [user("task"), call("giant"), out("giant", "g" * 200_000)]
    res = await pipeline.compact(req(entries, overflow=True))
    assert res.compacted is True
    marker = next(
        e
        for e in res.entries
        if isinstance(e, ToolResultEntry) and e.call_id == "giant"
    )
    assert "cleared to save context" in marker.output
    assert res.tokens_after < res.tokens_before


# ---------------------------------------------------------------------------
# Marker tailoring by tool availability
# ---------------------------------------------------------------------------


async def test_markers_omit_recall_hint_when_tool_absent():
    summarizer = FakeSummarizer()
    pipeline = _pipeline(context_window=10_000, summarizer=summarizer)
    entries: list = [user("task")]
    for i in range(20):
        entries += [call(f"c{i}"), out(f"c{i}", "r" * 2_000)]

    res = await pipeline.compact(req(entries, tool_names=frozenset({"other_tool"})))
    markers = [
        e.output
        for e in res.entries
        if getattr(e, "output", "").startswith("[Earlier tool result cleared")
    ]
    assert markers and all("recall_tool_result" not in m for m in markers)

    res2 = await pipeline.compact(
        req(entries, tool_names=frozenset({"recall_tool_result"}))
    )
    markers2 = [
        e.output
        for e in res2.entries
        if getattr(e, "output", "").startswith("[Earlier tool result cleared")
    ]
    assert markers2 and all("recall_tool_result" in m for m in markers2)


# ---------------------------------------------------------------------------
# View validity property: no orphan tool results, ever
# ---------------------------------------------------------------------------


def _assert_view_valid(view):
    """Provider-style validation: every tool result's call precedes it."""
    seen_calls: set[str] = set()
    for e in view:
        if isinstance(e, ToolCallEntry):
            seen_calls.add(e.call_id)
        elif isinstance(e, ToolResultEntry):
            assert e.call_id in seen_calls, f"orphan tool result {e.call_id!r}"


async def test_views_stay_provider_valid_across_growth_and_bursts():
    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=3_000, compact_at=0.5, compact_to=0.3, summarizer=summarizer
    )
    scratch: dict = {}
    entries: list = [user("the task, with details " * 5)]
    for i in range(15):
        # Interleave text and tool pairs, including back-to-back calls.
        entries += [call(f"a{i}"), call(f"b{i}")]
        entries += [out(f"a{i}", "r" * 600), out(f"b{i}", "r" * 600)]
        if i % 3 == 0:
            entries.append(user(f"intermediate question {i}"))
        res = await pipeline.compact(req(entries, scratch=scratch))
        _assert_view_valid(res.entries)
    assert CompactionState.load(scratch).summary is not None


async def test_absolute_watermarks_accepted():
    summarizer = FakeSummarizer()
    pipeline = Compaction(
        context_window=2_000,
        reserve_output_tokens=0,
        compact_at=900,  # absolute tokens
        compact_to=400,
        summarizer=summarizer,
    )
    entries = [user("x" * 100) for _ in range(60)]  # ~1980 tokens >= 900
    res = await pipeline.compact(req(entries))
    assert res.compacted is True
    small = [user("x" * 100) for _ in range(20)]  # ~660 < 900
    res2 = await pipeline.compact(req(small))
    assert res2.compacted is False
