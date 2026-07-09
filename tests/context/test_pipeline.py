"""Tests for Compaction: gating, hysteresis, stickiness, reactive path,
and runner integration."""

from __future__ import annotations

import json

import pytest

from lovia import (
    Agent,
    ContextOverflowError,
    Compaction,
    InMemorySession,
    Runner,
)
from lovia.transcript import AssistantTextEntry, InputEntry
from lovia.context import (
    CompactionState,
    ContextResult,
    NoopContextPolicy,
    OffloadRecord,
    SummaryState,
)
from lovia.context.state import fingerprint
from lovia.events import ContextCompacted
from lovia.transcript import ToolCallEntry, ToolResultEntry, entry_to_dict

from ..scripted_provider import ScriptedProvider, text
from .helpers import (
    FailingSummarizer,
    FakeProviderWithWindow,
    FakeResultStore,
    FakeSummarizer,
    call,
    out,
    req,
    system,
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


def test_counter_cache_keyed_by_provider_identity():
    # Guards the id()-reuse hazard: the counter cache must key on the provider
    # object (strong ref), not id(), so a collected provider's id can't be
    # reused by a different provider and hand back the wrong tokenizer.
    pipeline = Compaction(context_window=1_000)
    p1, p2 = FakeProviderWithWindow(), FakeProviderWithWindow()
    c1 = pipeline._counter_for(p1)
    assert pipeline._counter[0] is p1  # cached by identity, not id()
    assert pipeline._counter_for(p1) is c1  # same object -> reuse
    assert pipeline._counter_for(p2) is not c1  # distinct object -> rebuild


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
    res = await pipeline.compact(req(entries))

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
    calls_after_burst = len(summarizer.calls)

    second = await pipeline.compact(req(entries, scratch=scratch))
    assert second.compacted is False
    assert second.changed is True
    assert second.reason == "sticky_replay"
    assert len(summarizer.calls) == calls_after_burst  # no new LLM work
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
    store = FakeResultStore()
    pipeline = _pipeline(
        context_window=4_000,
        compact_at=0.5,
        compact_to=0.25,
        summarizer=summarizer,
        store=store,
    )
    entries: list = [user("task")]
    for i in range(4):
        entries += [call(f"c{i}"), out(f"c{i}", "A" * 6_000)]
    res = await pipeline.compact(req(entries))

    assert res.compacted is True
    assert "offload" in res.reason
    assert store.data  # something was archived
    for content in store.data.values():
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
# Fingerprint reset (covered prefix rewritten out from under the summary)
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
    calls_after_burst = len(summarizer.priors)

    rewritten = [user("REWRITTEN HISTORY " + "y" * 100), *entries[1:]]
    res = await pipeline.compact(req(rewritten, scratch=scratch))
    # The stale summary was dropped and rebuilt from scratch (prior=None on
    # the rebuild's first fold, not the carried summary text).
    assert summarizer.priors[0] is None
    assert summarizer.priors[calls_after_burst] is None
    assert res.compacted is True


async def test_pair_splitting_summary_coverage_is_rewound_on_load():
    """A persisted ``covered`` inside a call/result pair (written by an
    interrupted fold on an older lovia) must be rewound to the nearest
    pair-safe cut — otherwise every retry replays a view whose first
    post-summary entry is an orphaned tool result, which providers 400."""
    entries = [system("sys"), user("q")]
    for i in range(6):
        entries.append(call(f"c{i}"))
        entries.append(out(f"c{i}", f"result {i}"))
    body = entries[1:]

    broken = 8  # between call c3 and its result
    assert isinstance(body[broken - 1], ToolCallEntry)
    scratch: dict = {}
    CompactionState(
        summary=SummaryState(
            text="OLD SUMMARY", covered=broken, fingerprint=fingerprint(body[:broken])
        )
    ).save(scratch)

    pipeline = _pipeline(context_window=100_000)  # far below the watermark
    res = await pipeline.compact(req(entries, scratch=scratch))

    healed = CompactionState.load(scratch).summary
    assert healed is not None and healed.covered == 7
    assert healed.fingerprint == fingerprint(body[:7])
    # The view keeps the pair intact right after the summary entry.
    first_after_summary = res.entries[2]
    assert isinstance(first_after_summary, ToolCallEntry)
    assert first_after_summary.call_id == "c3"


async def test_pair_splitting_coverage_with_no_safe_cut_resets_summary():
    """When rewinding lands at 0 there is nothing left to cover: the summary
    is dropped entirely rather than kept with covered=0."""
    entries = [system("sys"), call("a"), out("a"), user("q")]
    body = entries[1:]
    scratch: dict = {}
    CompactionState(
        summary=SummaryState(text="OLD", covered=1, fingerprint=fingerprint(body[:1]))
    ).save(scratch)

    pipeline = _pipeline(context_window=100_000)
    res = await pipeline.compact(req(entries, scratch=scratch))
    assert CompactionState.load(scratch).summary is None
    assert res.entries == entries


async def test_protected_tail_measured_on_rendered_view():
    """A cleared result near the tail costs marker-size in the actual prompt,
    so it must not eat the tail budget at its raw size — that would leave the
    model less verbatim recency than ``keep_recent_tokens`` promises."""
    from lovia.context import SummarizeHistory

    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000,
        compact_at=0.9,
        compact_to=0.5,
        keep_recent_tokens=600,
        stages=[SummarizeHistory(summarizer=summarizer)],
    )
    body = [
        user("x" * 4_000),  # ~1000 tokens: exceeds the remaining tail budget
        call("c0"),
        out("c0", "r" * 8_000),  # cleared: renders as a ~35-token marker
        user("tail question"),
    ]
    scratch: dict = {}
    CompactionState(cleared={"c0"}).save(scratch)
    await pipeline.compact(req(body, scratch=scratch))
    covered = CompactionState.load(scratch).summary
    # Counted raw, the cleared 2000-token result would blow the 600-token
    # tail budget and the summary would cover the first three entries;
    # counted as rendered, only the big leading user message is summarized.
    assert covered is not None and covered.covered == 1


async def test_summary_survives_stored_output_trim():
    """An operator trimming stored tool outputs (``Session.trim_tool_results``)
    keeps entry structure and call ids; the result-length-blind fingerprint
    therefore matches and the carried summary is NOT reset."""
    from lovia.context import SummarizeHistory

    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000,
        compact_at=0.5,
        compact_to=0.3,
        stages=[SummarizeHistory(summarizer=summarizer)],
    )
    scratch: dict = {}
    entries: list = [user("go")]
    for i in range(10):
        entries += [call(f"c{i}"), out(f"c{i}", "r" * 500)]
    await pipeline.compact(req(entries, scratch=scratch))
    before = CompactionState.load(scratch).summary
    assert before is not None
    calls_after_burst = len(summarizer.calls)

    trimmed = [
        ToolResultEntry(call_id=e.call_id, output=e.output[:50] + "[trimmed]")
        if isinstance(e, ToolResultEntry)
        else e
        for e in entries
    ]
    await pipeline.compact(req(trimmed, scratch=scratch))
    after = CompactionState.load(scratch).summary
    assert after is not None and after.covered == before.covered  # not reset
    assert len(summarizer.calls) == calls_after_burst  # no re-summarize


async def test_leading_system_run_swap_keeps_summary():
    # A handoff swaps/adds a leading system entry but leaves the body intact.
    # split_system collapses the whole leading system *run*, so the body — and
    # the body-relative summary coverage — stays invariant; the running summary
    # survives (re-used as the prior) instead of being reset like a real prefix
    # rewrite (contrast the test above).
    summarizer = FakeSummarizer()
    pipeline = _pipeline(
        context_window=1_000, compact_at=0.5, compact_to=0.3, summarizer=summarizer
    )
    scratch: dict = {}
    body = [user("x" * 100) for _ in range(30)]
    # Systemless agent carrying a caller-supplied leading system() input.
    await pipeline.compact(req([system("CALLER"), *body], scratch=scratch))
    before = CompactionState.load(scratch).summary
    assert before is not None

    # Handoff to a systemful agent: a SECOND leading system appears, body intact.
    await pipeline.compact(
        req([system("AGENT"), system("CALLER"), *body], scratch=scratch)
    )
    after = CompactionState.load(scratch).summary

    # Summary survived byte-for-byte (NOT reset): same coverage, fingerprint, and
    # text — so the swap was a no-op for compaction. And no summarization
    # restarted from scratch after the handoff (contrast the reset test's
    # priors == [None, None]).
    assert after == before
    assert None not in summarizer.priors[1:]


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


def _scratch_with_learned(window: int, key: str = "\x00m") -> dict:
    scratch: dict = {}
    CompactionState(learned_windows={key: window}).save(scratch)
    return scratch


async def test_overflow_teaches_the_window_and_it_enables_proactive_compaction():
    """One overflow is the whole price of an unknown model."""
    pipeline = Compaction(summarizer=FakeSummarizer())
    provider = FakeProviderWithWindow(window=None)  # adapter has no idea
    entries = [user("x" * 100) for _ in range(30)]  # ~990 tokens
    scratch: dict = {}

    # Nothing known: no budget at all, so the policy stays out of the way.
    before = await pipeline.compact(
        req(entries, provider=provider, model="m", scratch=scratch)
    )
    assert before.compacted is False
    assert not any("context was" in d for d in before.detail)

    # The endpoint rejects the prompt and names its limit.
    await pipeline.compact(
        req(
            entries,
            provider=provider,
            model="m",
            scratch=scratch,
            overflow=True,
            reported_window=2_048,
        )
    )
    assert CompactionState.load(scratch).learned_windows == {"\x00m": 2_048}

    # From now on the policy budgets against the real window.
    after = await pipeline.compact(
        req(entries, provider=provider, model="m", scratch=_scratch_with_learned(2_048))
    )
    assert after.compacted is True  # usable = 1024, trigger 768 < 990
    assert any("context was" in d for d in after.detail)


async def test_learned_window_caps_an_overstated_claim():
    pipeline = Compaction(summarizer=FakeSummarizer())
    provider = FakeProviderWithWindow(window=10_000_000)  # wildly overstated
    entries = [user("x" * 100) for _ in range(30)]

    # Untouched, the 10M claim keeps the trigger far out of reach.
    unclamped = await pipeline.compact(
        req(entries, provider=provider, model="m", scratch={})
    )
    assert unclamped.compacted is False

    clamped = await pipeline.compact(
        req(
            entries,
            provider=provider,
            model="m",
            scratch=_scratch_with_learned(2_048),
        )
    )
    assert clamped.compacted is True


async def test_learned_window_never_raises_a_deliberately_smaller_budget():
    """``min`` respects a user who budgets below the real window."""
    pipeline = Compaction(context_window=1_000, summarizer=FakeSummarizer())
    entries = [user("x" * 100) for _ in range(30)]
    res = await pipeline.compact(
        req(entries, model="m", scratch=_scratch_with_learned(1_000_000))
    )
    assert res.compacted is True  # still sized to the configured 1_000


async def test_learned_window_is_scoped_to_its_own_endpoint_and_model():
    """A window learned for one model must not size another."""
    pipeline = Compaction(summarizer=FakeSummarizer())
    entries = [user("x" * 100) for _ in range(30)]
    res = await pipeline.compact(
        req(entries, model="other", scratch=_scratch_with_learned(2_048, "\x00m"))
    )
    assert res.compacted is False


async def test_learned_window_is_keyed_by_endpoint_and_model():
    pipeline = Compaction(summarizer=FakeSummarizer())
    entries = [user("x") for _ in range(3)]
    scratch: dict = {}
    for model in ("a", "b"):
        await pipeline.compact(
            req(
                entries,
                model=model,
                scratch=scratch,
                overflow=True,
                reported_window=4_096 if model == "a" else 8_192,
            )
        )
    assert CompactionState.load(scratch).learned_windows == {
        "\x00a": 4_096,
        "\x00b": 8_192,
    }


async def test_latest_reported_window_replaces_the_previous_one():
    """A resized deployment restates its limit; the newest wins."""
    pipeline = Compaction(summarizer=FakeSummarizer())
    entries = [user("x") for _ in range(3)]
    scratch: dict = {}
    for reported in (4_096, 8_192):
        await pipeline.compact(
            req(
                entries,
                model="m",
                scratch=scratch,
                overflow=True,
                reported_window=reported,
            )
        )
    assert CompactionState.load(scratch).learned_windows == {"\x00m": 8_192}


async def test_reactive_summarizer_failure_is_contained_and_persists_counter():
    # The summarizer's own error stays inside the pipeline (the runner must
    # get to surface the original ContextOverflowError); the failure counter
    # is still recorded.
    pipeline = Compaction(summarizer=FailingSummarizer())
    scratch: dict = {}
    entries = [user("x" * 1_000) for _ in range(4)]
    res = await pipeline.compact(req(entries, overflow=True, scratch=scratch))
    assert res.compacted is False
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
    calls_after_burst = len(summarizer.calls)

    revived_scratch = json.loads(json.dumps(scratch))
    res = await pipeline.compact(req(entries, scratch=revived_scratch))
    assert res.compacted is False  # decisions replayed, none re-made
    assert [entry_to_dict(e) for e in res.entries] == [
        entry_to_dict(e) for e in first.entries
    ]
    assert len(summarizer.calls) == calls_after_burst


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

    def context_window(self) -> int | None:
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
    assert summarizer.calls  # the reactive burst summarized (maybe chunked)
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
    assert compacted[0].notice.reactive is True
    assert compacted[0].notice.reason == "reactive_summary"
    assert compacted[0].notice.summary == "S."
    assert isinstance(compacted[0].notice.tokens_after, int)


async def test_runner_persists_compaction_notice_to_segment_meta():
    """A run that compacts stows a JSON-safe notice in its finished segment's
    meta, so the web UI can replay it when the session is reloaded."""
    from lovia.session import NOTICE_META_KEY

    policy = Compaction(summarizer=FakeSummarizer("S."))
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    sess = await _seeded_session()
    async for _ in Runner.stream(
        agent, "go", context_policy=policy, session=sess, session_id="s1"
    ):
        pass
    notice = (await sess.segments("s1"))[-1].meta[NOTICE_META_KEY]
    assert notice["reason"] == "reactive_summary"
    assert notice["reactive"] is True
    assert notice["summary"] == "S."
    # The token numbers ride along at the top level now (no nested metadata).
    assert notice["tokens_before"] >= notice["tokens_after"]


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
    assert compacted[0].notice.reactive is True
    assert "hello after compaction" in (events_seen[-1].result.output or "")


async def test_runner_overflow_on_incompressible_prompt_propagates():
    """A 2-entry prompt has nothing to compact; the overflow must surface."""
    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    with pytest.raises(ContextOverflowError):
        await Runner.run(agent, "hello")


async def test_reactive_compact_gets_no_stale_calibration_sample():
    """The retry compact() must not see ``last_input_tokens``: this turn's
    first compact already consumed it, and it describes the previous turn's
    view — pairing it with the just-overflowed (larger) view would drag the
    calibration ratio down."""
    from lovia import tool

    from ..scripted_provider import call as provider_call

    @tool
    async def ping() -> str:
        return "pong"

    class OverflowOnSecondStream(ScriptedProvider):
        def __init__(self, script) -> None:
            super().__init__(script)
            self.streams = 0

        async def stream(self, entries, **kwargs):
            self.streams += 1
            if self.streams == 2:
                raise ContextOverflowError("simulated overflow")
            async for delta in super().stream(entries, **kwargs):
                yield delta

    seen: list[tuple[bool, int | None]] = []

    class SpyPolicy:
        async def compact(self, request):
            seen.append((request.overflow, request.last_input_tokens))
            return ContextResult(
                entries=list(request.entries),
                changed=request.overflow,
                compacted=request.overflow,
                tokens_after=10 if request.overflow else 1_000,
            )

    provider = OverflowOnSecondStream([provider_call("ping", {}), text("done")])
    agent = Agent(name="t", instructions="x", model=provider, tools=[ping])
    result = await Runner.run(agent, "go", context_policy=SpyPolicy())
    assert result.output == "done"
    assert seen[0] == (False, None)  # turn 1: nothing observed yet
    assert seen[1][0] is False and seen[1][1] is not None  # turn 2: calibrates
    assert seen[2] == (True, None)  # retry: stale sample withheld


async def test_runner_skips_doomed_retry_when_view_barely_shrinks():
    """A reactive view that is not meaningfully smaller than the one that
    just failed would hit the same 400 — the runner surfaces the overflow
    instead of paying for the retry."""

    class BarelyShrinkingPolicy:
        async def compact(self, request):
            if not request.overflow:
                return ContextResult(entries=list(request.entries), tokens_after=1_000)
            return ContextResult(
                entries=list(request.entries),
                changed=True,
                compacted=True,
                tokens_after=990,  # under 5% smaller than the failed 1_000
            )

    provider = _OverflowOnceProvider()
    agent = Agent(name="t", instructions="x", model=provider)
    with pytest.raises(ContextOverflowError):
        await Runner.run(agent, "hello", context_policy=BarelyShrinkingPolicy())
    assert provider.stream_count == 1  # the doomed retry was never sent


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
# Cross-run continuation (the full policy scratch persisted in the segment meta)
# ---------------------------------------------------------------------------


def test_state_round_trips_full_including_calibration():
    """There is no carryover subset anymore: the policy carries its FULL scratch
    across runs — decisions AND calibration — via the one ``save``/``load`` shape
    the checkpoint and the session-meta both use."""
    state = CompactionState(
        cleared={"a", "b"},
        offloaded={"c": OffloadRecord(preview="p", chars=99)},
        summary=SummaryState(text="S", covered=3, fingerprint="fp"),
        ratio=2.5,
        last_view_estimate=999,
        summary_failures=2,
    )
    scratch: dict = {}
    state.save(scratch)
    # Everything survives verbatim — including the carried ``ratio`` (better kept
    # than re-learned) and the ``summary_failures`` breaker count.
    assert CompactionState.load(scratch) == state


async def test_continuation_resumes_summary_across_runs_durably():
    """A *fresh* policy instance on the second run inherits the first run's
    summary from the session segment meta — durable continuation, not an
    in-process cache — so the long prefix is not re-summarized."""
    from lovia.session import STATE_META_KEY

    sess = InMemorySession()
    await sess.append("s1", [user(f"m{i}" + "x" * 98) for i in range(30)])
    provider = ScriptedProvider([text("a1"), text("a2")])
    agent = Agent(name="t", instructions="x", model=provider)
    summarizer = FakeSummarizer("History summarized.")

    def fresh_policy() -> Compaction:
        return Compaction(
            context_window=1_000,
            reserve_output_tokens=0,
            compact_at=0.5,
            compact_to=0.3,
            summarizer=summarizer,
        )

    await Runner.run(
        agent, "q1", context_policy=fresh_policy(), session=sess, session_id="s1"
    )
    calls_after_first = len(summarizer.calls)
    assert calls_after_first >= 1  # run 1 summarized the long prefix

    # The completed run wrote its full policy state into the latest segment meta.
    segs = await sess.segments("s1")
    assert segs[-1].meta and STATE_META_KEY in segs[-1].meta
    # The FULL scratch carries — not a decisions-only subset. Calibration and the
    # summarizer breaker ride along too (the exact inverse of the old carryover,
    # which dropped them).
    carried = segs[-1].meta[STATE_META_KEY]["context"]
    assert {"ratio", "last_view_estimate", "summary_failures"} <= carried.keys()

    await Runner.run(
        agent, "q2", context_policy=fresh_policy(), session=sess, session_id="s1"
    )
    assert len(summarizer.calls) == calls_after_first  # inherited; no re-summarize


async def test_stale_and_ambiguous_records_are_pruned_from_scratch():
    """Records for ids the body no longer holds (trimmed history) or holds
    twice (a provider reusing call ids) are GC'd instead of replayed —
    a reused id must not render the newest result as a marker."""
    entries = [
        user("go"),
        call("call_0"),
        out("call_0", "old " * 200),
        call("call_0"),
        out("call_0", "new " * 200),
    ]
    scratch: dict = {}
    CompactionState(
        cleared={"ghost", "call_0"},
        offloaded={"ghost2": OffloadRecord(preview="p", chars=9)},
    ).save(scratch)
    pipeline = _pipeline(context_window=1_000_000)
    res = await pipeline.compact(req(entries, scratch=scratch))
    assert res.changed is False  # nothing rendered as a marker
    pruned = CompactionState.load(scratch)
    assert pruned.cleared == set() and pruned.offloaded == {}


async def test_detail_bullets_describe_what_changed():
    """The policy authors its own notice bullets from its state (the UI renders
    them verbatim). Covers ``_plural`` both ways and the pressure line."""
    pipeline = _pipeline(context_window=100_000)
    scratch: dict = {}
    CompactionState(
        cleared={"a"},
        offloaded={
            "c": OffloadRecord(preview="p", chars=9),
            "d": OffloadRecord(preview="q", chars=9),
        },
    ).save(scratch)
    entries = [
        user("hi"),
        call("a"),
        out("a", "x"),
        call("c"),
        out("c", "x"),
        call("d"),
        out("d", "x"),
    ]
    res = await pipeline.compact(req(entries, scratch=scratch))
    assert "2 tool results offloaded in total" in res.detail  # plural
    assert "1 tool result cleared in total" in res.detail  # singular
    assert any(b.endswith("% full") for b in res.detail)  # pressure line present


async def test_custom_policy_detail_flows_to_the_notice():
    """A non-Compaction policy authors its own notice bullets; the loop forwards
    them verbatim into the event (hence into the UI and the segment meta). Proves
    the notice is policy-agnostic — the UI never reaches into Compaction internals
    — and doubles as a minimal-policy smoke test (only ``compact`` implemented)."""

    class KeepTailPolicy:
        async def compact(self, request):
            return ContextResult(
                entries=request.entries[-1:],
                changed=True,
                compacted=True,
                reason="keep_tail",
                tokens_before=100,
                tokens_after=10,
                detail=["dropped everything but the tail"],
            )

    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="t", instructions="x", model=provider)
    seen: list = []
    async for ev in Runner.stream(agent, "go", context_policy=KeepTailPolicy()):
        seen.append(ev)
    compacted = [e for e in seen if isinstance(e, ContextCompacted)]
    assert len(compacted) == 1
    assert compacted[0].notice.reason == "keep_tail"
    assert compacted[0].notice.detail == ["dropped everything but the tail"]
    assert compacted[0].notice.tokens_before == 100


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
    # Offload now runs without a store, so it claims the oversized latest result
    # first — a preview marker rather than clear's bare one; the retry fits either
    # way.
    assert "trimmed to a preview to save context" in marker.output
    assert res.tokens_after < res.tokens_before


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
