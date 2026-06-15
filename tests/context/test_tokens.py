"""Tests for token estimation and window watermarks."""

from __future__ import annotations

import weakref

import pytest

from lovia.parts import ImagePart, TextPart
from lovia.context import TokenBudget, TokenCounter
from lovia.transcript import (
    AssistantTextEntry,
    InputEntry,
    ReasoningEntry,
    ToolCallEntry,
    ToolResultEntry,
)

from .helpers import user

# ---------------------------------------------------------------------------
# TokenCounter estimates
# ---------------------------------------------------------------------------


def test_count_entry_text_entries():
    counter = TokenCounter()
    assert counter.count_entry(user("x" * 400)) == 108  # 400//4 + 8
    assert counter.count_entry(AssistantTextEntry(content="y" * 40)) == 18
    assert counter.count_entry(ReasoningEntry(content="z" * 80)) == 28


def test_count_entry_tool_entries():
    counter = TokenCounter()
    call = ToolCallEntry(call_id="c1", name="search", arguments='{"q": "pandas"}')
    assert counter.count_entry(call) == (len("search") + len(call.arguments)) // 4 + 8
    result = ToolResultEntry(call_id="c1", output="r" * 1000)
    assert counter.count_entry(result) == 258


def test_image_part_gets_flat_cost_not_base64_chars():
    counter = TokenCounter()
    huge_base64 = "A" * 1_000_000
    entry = InputEntry(
        role="user",
        content=[
            TextPart(text="describe this"),
            ImagePart(data=huge_base64, mime_type="image/png"),
        ],
    )
    estimate = counter.count_entry(entry)
    assert estimate == 8 + len("describe this") // 4 + 1_600
    # Orders of magnitude below the naive chars//4 of the base64 payload.
    assert estimate < 1_000_000 // 4 / 10


def test_count_sums_entries():
    counter = TokenCounter()
    entries = [user("x" * 40), user("y" * 40)]
    assert counter.count(entries) == 2 * (10 + 8)


# ---------------------------------------------------------------------------
# Memoization
# ---------------------------------------------------------------------------


class _CountingEstimator:
    """Provider stub with a tokenizer; counts how often it is consulted."""

    def __init__(self) -> None:
        self.calls = 0

    def estimate_tokens(self, entries) -> int:
        self.calls += 1
        return 42


def test_provider_estimator_is_dispatched_and_memoized():
    estimator = _CountingEstimator()
    counter = TokenCounter(estimator)
    entry = user("hello")
    assert counter.count_entry(entry) == 42
    assert counter.count_entry(entry) == 42
    assert estimator.calls == 1  # second hit served from the memo


def test_memo_guard_rejects_stale_id_reuse():
    counter = TokenCounter()
    original = user("aaaa")
    fresh = user("x" * 400)
    # Simulate id() reuse after GC: a memo slot keyed by the *new* entry's id
    # but holding a weakref to a different object must not be trusted.
    counter._memo[id(fresh)] = (weakref.ref(original), 999)
    assert counter.count_entry(fresh) == 108


def test_memo_is_bounded():
    counter = TokenCounter(memo_size=4)
    entries = [user(f"m{i}") for i in range(10)]
    for entry in entries:
        counter.count_entry(entry)
    assert len(counter._memo) <= 4


def test_broken_provider_estimator_falls_back_to_heuristic():
    class _Broken:
        def estimate_tokens(self, entries) -> int:
            raise RuntimeError("tokenizer exploded")

    counter = TokenCounter(_Broken())
    assert counter.count_entry(user("x" * 400)) == 108


# ---------------------------------------------------------------------------
# TokenBudget
# ---------------------------------------------------------------------------


def test_budget_watermarks():
    budget = TokenBudget(window=1_000, reserve_output=0, trigger=0.75, target=0.5)
    assert budget.usable == 1_000
    assert budget.trigger_tokens == 750
    assert budget.target_tokens == 500
    assert budget.pressure(500) == 0.5


def test_budget_reserve_subtracted():
    budget = TokenBudget(window=200_000, reserve_output=16_384)
    assert budget.usable == 200_000 - 16_384


def test_budget_reserve_larger_than_window_falls_back_to_half():
    budget = TokenBudget(window=2_000)  # default reserve 16_384 >= window
    assert budget.usable == 1_000


def test_budget_validation():
    with pytest.raises(ValueError, match="window"):
        TokenBudget(window=0)
    with pytest.raises(ValueError, match="trigger"):
        TokenBudget(window=100, trigger=1.5)
    with pytest.raises(ValueError, match="target"):
        TokenBudget(window=100, trigger=0.5, target=0.5)
    with pytest.raises(ValueError, match="reserve_output"):
        TokenBudget(window=100, reserve_output=-1)


def test_budget_absolute_watermarks():
    budget = TokenBudget(
        window=200_000, reserve_output=0, trigger=150_000, target=100_000
    )
    assert budget.trigger_tokens == 150_000
    assert budget.target_tokens == 100_000


def test_budget_absolute_watermarks_clamp_to_usable():
    # Thresholds above the actual window degrade gracefully instead of
    # never firing / never terminating.
    budget = TokenBudget(
        window=10_000, reserve_output=0, trigger=150_000, target=100_000
    )
    assert budget.trigger_tokens == 10_000
    assert budget.target_tokens == 9_999  # capped below trigger: hysteresis survives


def test_budget_mixed_watermarks_resolve_at_runtime():
    budget = TokenBudget(window=100_000, reserve_output=0, trigger=0.9, target=20_000)
    assert budget.trigger_tokens == 90_000
    assert budget.target_tokens == 20_000


def test_budget_same_type_ordering_validated():
    with pytest.raises(ValueError, match="below trigger"):
        TokenBudget(window=100, trigger=50, target=60)
