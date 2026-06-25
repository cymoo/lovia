"""Unit tests for the individual compaction stages."""

from __future__ import annotations

import pytest

from lovia.context import (
    ClearToolResults,
    CompactionRequest,
    CompactionState,
    OffloadToolResults,
    StageContext,
    SummarizeHistory,
    SummaryState,
    TokenBudget,
    TokenCounter,
)
from lovia.context.state import fingerprint
from lovia.transcript import ToolCallEntry

from .helpers import (
    FailingResultStore,
    FailingSummarizer,
    FakeResultStore,
    FakeSummarizer,
    call,
    out,
    user,
)


def make_ctx(
    body,
    *,
    state: CompactionState | None = None,
    store=None,
    aggressive: bool = False,
    budget: TokenBudget | None = None,
    protected_from: int | None = None,
):
    state = state if state is not None else CompactionState()
    counter = TokenCounter()
    return StageContext(
        request=CompactionRequest(entries=list(body)),
        state=state,
        counter=counter,
        budget=budget
        or TokenBudget(window=1_000, reserve_output=0, trigger=0.75, target=0.5),
        current_tokens=counter.count(body),
        protected_from=len(body) if protected_from is None else protected_from,
        aggressive=aggressive,
        store=store,
    )


def _pairs(n: int, *, chars: int = 1_000, name: str = "f"):
    body = []
    for i in range(n):
        body.append(call(f"c{i}", name))
        body.append(out(f"c{i}", "r" * chars))
    return body


# ---------------------------------------------------------------------------
# ClearToolResults
# ---------------------------------------------------------------------------


async def test_clear_keeps_recent_results_and_clears_oldest_first():
    body = _pairs(5)
    ctx = make_ctx(body)
    decided = await ClearToolResults(keep_last=3).plan(body, ctx)
    assert decided is True
    assert ctx.state.cleared == {"c0", "c1"}  # last 3 protected


async def test_clear_stops_once_under_target():
    body = _pairs(10)  # ~2660 tokens
    budget = TokenBudget(window=2_000, reserve_output=0, trigger=0.9, target=0.8)
    ctx = make_ctx(body, budget=budget)  # target 1600
    await ClearToolResults(keep_last=0).plan(body, ctx)
    # Clearing all 10 would free ~2250; reaching 1600 needs ~5 clears.
    assert 4 <= len(ctx.state.cleared) <= 6
    assert "c9" not in ctx.state.cleared or len(ctx.state.cleared) < 10


async def test_clear_skips_small_results():
    body = _pairs(5, chars=10)
    ctx = make_ctx(body)
    assert await ClearToolResults(keep_last=0, min_chars=200).plan(body, ctx) is False
    assert ctx.state.cleared == set()


async def test_clear_respects_exclude_tools():
    body = _pairs(5, name="memory")
    ctx = make_ctx(body)
    stage = ClearToolResults(keep_last=0, exclude_tools=["memory"])
    assert await stage.plan(body, ctx) is False


async def test_clear_respects_protected_tail():
    body = _pairs(5)
    ctx = make_ctx(body, protected_from=2)  # only the first pair unprotected
    await ClearToolResults(keep_last=0).plan(body, ctx)
    assert ctx.state.cleared == {"c0"}


async def test_clear_aggressive_overrides():
    body = _pairs(3, chars=10)  # tiny outputs, normally skipped
    # Realistic budget: tiny results are not individually oversized, so the
    # aggressive keep_last=1 retention still protects the newest result.
    budget = TokenBudget(window=40, reserve_output=0, trigger=0.9, target=0.5)
    ctx = make_ctx(body, aggressive=True, budget=budget)
    assert await ClearToolResults(keep_last=3, min_chars=200).plan(body, ctx) is True
    # Aggressive keeps only the most recent result.
    assert ctx.state.cleared == {"c0", "c1"}


async def test_clear_is_sticky_and_does_not_redecide():
    body = _pairs(5)
    state = CompactionState()
    ctx = make_ctx(body, state=state)
    await ClearToolResults(keep_last=3).plan(body, ctx)
    before = set(state.cleared)
    ctx2 = make_ctx(body, state=state)
    assert await ClearToolResults(keep_last=3).plan(body, ctx2) is False
    assert state.cleared == before


async def test_clear_at_least_frees_tokens_even_under_target():
    body = _pairs(5)  # ~1330 tokens, well under the huge target
    budget = TokenBudget(window=100_000, reserve_output=0, trigger=0.75, target=0.5)
    ctx = make_ctx(body, budget=budget)
    stage = ClearToolResults(keep_last=0, clear_at_least_tokens=100)
    assert await stage.plan(body, ctx) is True
    assert len(ctx.state.cleared) == 1  # one clear frees ~225 >= 100


# ---------------------------------------------------------------------------
# OffloadToolResults
# ---------------------------------------------------------------------------


async def test_offload_writes_store_and_records_marker_data():
    store = FakeResultStore()
    body = _pairs(2, chars=5_000)
    budget = TokenBudget(window=100, reserve_output=0, trigger=0.9, target=0.5)
    ctx = make_ctx(body, store=store, budget=budget)
    stage = OffloadToolResults(min_chars=4_000, keep_last=1)
    assert await stage.plan(body, ctx) is True
    assert store.data["c0"] == "r" * 5_000
    record = ctx.state.offloaded["c0"]
    assert record.preview == "r" * 400
    assert record.chars == 5_000
    assert "c1" not in ctx.state.offloaded  # keep_last=1


async def test_offload_inert_without_store():
    body = _pairs(3, chars=5_000)
    ctx = make_ctx(body, store=None)
    assert await OffloadToolResults(keep_last=0).plan(body, ctx) is False


async def test_offload_skips_small_results():
    store = FakeResultStore()
    body = _pairs(3, chars=100)
    ctx = make_ctx(body, store=store)
    assert (
        await OffloadToolResults(min_chars=4_000, keep_last=0).plan(body, ctx) is False
    )
    assert store.data == {}


async def test_offload_store_failure_skips_entry_without_raising():
    body = _pairs(2, chars=5_000)
    budget = TokenBudget(window=100, reserve_output=0, trigger=0.9, target=0.5)
    ctx = make_ctx(body, store=FailingResultStore(), budget=budget)
    assert await OffloadToolResults(keep_last=0).plan(body, ctx) is False
    assert ctx.state.offloaded == {}


async def test_offload_keys_store_by_raw_call_id():
    store = FakeResultStore()
    body = [
        ToolCallEntry(call_id="a/b:c", name="f", arguments="{}"),
        out("a/b:c", "r" * 5_000),
        call("recent"),
        out("recent", "r" * 5_000),
    ]
    budget = TokenBudget(window=100, reserve_output=0, trigger=0.9, target=0.5)
    ctx = make_ctx(body, store=store, budget=budget)
    await OffloadToolResults(keep_last=1).plan(body, ctx)
    assert "a/b:c" in ctx.state.offloaded
    assert store.data["a/b:c"] == "r" * 5_000


# ---------------------------------------------------------------------------
# SummarizeHistory
# ---------------------------------------------------------------------------


def _texts(n: int, chars: int = 400):
    return [user("x" * chars) for _ in range(n)]


async def test_summarize_first_summary_covers_unprotected_prefix():
    summarizer = FakeSummarizer("S1")
    body = _texts(10)
    ctx = make_ctx(body, protected_from=8)
    stage = SummarizeHistory(summarizer=summarizer)
    assert await stage.plan(body, ctx) is True
    assert summarizer.priors == [None]
    assert summarizer.calls[0] == body[:8]
    summary = ctx.state.summary
    assert summary is not None
    assert summary.text == "S1"
    assert summary.covered == 8
    assert summary.fingerprint == fingerprint(body[:8])


async def test_summarize_folds_only_the_new_span():
    summarizer = FakeSummarizer("S2")
    body = _texts(10)
    state = CompactionState(
        summary=SummaryState(text="S1", covered=4, fingerprint=fingerprint(body[:4]))
    )
    ctx = make_ctx(body, state=state, protected_from=8)
    assert await SummarizeHistory(summarizer=summarizer).plan(body, ctx) is True
    assert summarizer.priors == ["S1"]
    assert summarizer.calls[0] == body[4:8]
    assert state.summary is not None and state.summary.covered == 8


async def test_summarize_skips_when_no_new_coverage():
    summarizer = FakeSummarizer()
    body = _texts(10)
    state = CompactionState(
        summary=SummaryState(text="S1", covered=8, fingerprint=fingerprint(body[:8]))
    )
    ctx = make_ctx(body, state=state, protected_from=8)
    assert await SummarizeHistory(summarizer=summarizer).plan(body, ctx) is False
    assert summarizer.calls == []


async def test_summarize_anti_thrash_skips_marginal_savings():
    summarizer = FakeSummarizer()
    body = _texts(10, chars=40)  # tiny span: projected savings are negative
    ctx = make_ctx(body, protected_from=8)
    assert await SummarizeHistory(summarizer=summarizer).plan(body, ctx) is False
    assert summarizer.calls == []


async def test_summarize_aggressive_bypasses_anti_thrash():
    summarizer = FakeSummarizer()
    body = _texts(10, chars=40)
    ctx = make_ctx(body, protected_from=8, aggressive=True)
    assert await SummarizeHistory(summarizer=summarizer).plan(body, ctx) is True
    assert len(summarizer.calls) == 1


async def test_summarize_rejects_empty_summary():
    # A custom summarizer returning "" must NOT silently blank the prefix.
    body = _texts(10)
    ctx = make_ctx(body, protected_from=8)
    assert await SummarizeHistory(summarizer=FakeSummarizer("")).plan(body, ctx) is False
    assert ctx.state.summary is None
    assert ctx.state.summary_failures == 1


async def test_summarize_rejects_oversized_summary():
    # A summary over the cap is rejected (it's replayed into every view).
    body = _texts(10)
    ctx = make_ctx(body, protected_from=8)
    stage = SummarizeHistory(summarizer=FakeSummarizer("S" * 50), max_summary_chars=10)
    assert await stage.plan(body, ctx) is False
    assert ctx.state.summary is None
    assert ctx.state.summary_failures == 1


async def test_summarize_passes_rendered_span_with_markers():
    summarizer = FakeSummarizer()
    body = [call("c1"), out("c1", "x" * 2_000), *_texts(4)]
    state = CompactionState(cleared={"c1"})
    ctx = make_ctx(body, state=state, protected_from=4, aggressive=True)
    await SummarizeHistory(summarizer=summarizer).plan(body, ctx)
    span = summarizer.calls[0]
    assert any("cleared to save context" in getattr(e, "output", "") for e in span)
    assert not any(getattr(e, "output", "") == "x" * 2_000 for e in span)


async def test_summarize_circuit_breaker_blocks_after_failures():
    summarizer = FakeSummarizer()
    body = _texts(10)
    state = CompactionState(summary_failures=3)
    ctx = make_ctx(body, state=state, protected_from=8)
    assert await SummarizeHistory(summarizer=summarizer).plan(body, ctx) is False
    assert summarizer.calls == []


async def test_summarize_failure_increments_counter_then_success_resets():
    failing = FailingSummarizer()
    body = _texts(10)
    state = CompactionState()
    ctx = make_ctx(body, state=state, protected_from=8)
    assert await SummarizeHistory(summarizer=failing).plan(body, ctx) is False
    assert state.summary_failures == 1 and failing.calls == 1

    ctx2 = make_ctx(body, state=state, protected_from=8)
    assert await SummarizeHistory(summarizer=FakeSummarizer()).plan(body, ctx2) is True
    assert state.summary_failures == 0


async def test_summarize_reactive_failure_propagates():
    body = _texts(10)
    state = CompactionState()
    ctx = make_ctx(body, state=state, protected_from=8, aggressive=True)
    with pytest.raises(RuntimeError, match="boom"):
        await SummarizeHistory(summarizer=FailingSummarizer()).plan(body, ctx)
    assert state.summary_failures == 1


def test_stage_parameter_validation():
    with pytest.raises(ValueError, match="min_chars"):
        OffloadToolResults(min_chars=0)
    with pytest.raises(ValueError, match="keep_last"):
        ClearToolResults(keep_last=-1)
    with pytest.raises(ValueError, match="min_savings_ratio"):
        SummarizeHistory(summarizer=FakeSummarizer(), min_savings_ratio=1.0)
    with pytest.raises(ValueError, match="max_failures"):
        SummarizeHistory(summarizer=FakeSummarizer(), max_failures=0)


# ---------------------------------------------------------------------------
# Aggressive oversized exemption & read-only workspaces
# ---------------------------------------------------------------------------


async def test_clear_aggressive_drops_oversized_result_despite_protection():
    """A result that alone blows the target loses keep-last/tail immunity."""
    body = [call("giant"), out("giant", "g" * 40_000)]  # ~10K tokens
    budget = TokenBudget(window=4_000, reserve_output=0, trigger=0.75, target=0.5)
    ctx = make_ctx(body, aggressive=True, budget=budget, protected_from=0)
    assert await ClearToolResults(keep_last=3).plan(body, ctx) is True
    assert ctx.state.cleared == {"giant"}


async def test_clear_proactive_never_touches_protected_results():
    body = [call("giant"), out("giant", "g" * 40_000)]
    budget = TokenBudget(window=4_000, reserve_output=0, trigger=0.75, target=0.5)
    ctx = make_ctx(body, aggressive=False, budget=budget, protected_from=0)
    assert await ClearToolResults(keep_last=3).plan(body, ctx) is False
    assert ctx.state.cleared == set()


async def test_offload_aggressive_archives_oversized_protected_result():
    store = FakeResultStore()
    body = [call("giant"), out("giant", "g" * 40_000)]
    budget = TokenBudget(window=4_000, reserve_output=0, trigger=0.75, target=0.5)
    ctx = make_ctx(
        body, store=store, aggressive=True, budget=budget, protected_from=0
    )
    assert await OffloadToolResults(keep_last=2).plan(body, ctx) is True
    assert store.data["giant"] == "g" * 40_000
