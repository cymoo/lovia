"""Tests for sticky compaction state (de)serialization and fingerprints."""

from __future__ import annotations

import json

from lovia.context import CompactionState, OffloadRecord, SummaryState
from lovia.context.state import fingerprint
from .helpers import call, out, user


def _full_state() -> CompactionState:
    return CompactionState(
        cleared={"c1", "c2"},
        offloaded={"c3": OffloadRecord(preview="pre", chars=9000)},
        summary=SummaryState(text="S", covered=4, fingerprint="ab" * 8),
        ratio=1.5,
        last_view_estimate=1234,
        summary_failures=1,
    )


def test_state_round_trips_through_scratch():
    scratch: dict = {}
    _full_state().save(scratch)
    loaded = CompactionState.load(scratch)
    assert loaded == _full_state()


def test_state_round_trips_through_checkpoint_json():
    """Exact checkpoint path: context_policy_state serialized as JSON and back."""
    scratch: dict = {}
    _full_state().save(scratch)
    revived = json.loads(json.dumps(scratch))
    loaded = CompactionState.load(revived)
    assert loaded == _full_state()


def test_load_tolerates_missing_and_garbage():
    assert CompactionState.load({}) == CompactionState()
    assert CompactionState.load({"context": "garbage"}) == CompactionState()
    assert CompactionState.load({"context": {"version": 99}}) == CompactionState()
    partial = {"context": {"version": 2, "cleared": ["a", 7], "ratio": "NaN"}}
    state = CompactionState.load(partial)
    assert state.cleared == {"a"}
    assert state.ratio == 1.0


def test_load_clamps_ratio():
    state = CompactionState.load({"context": {"version": 2, "ratio": 100.0}})
    assert state.ratio == 4.0


def test_decided_covers_both_kinds():
    state = CompactionState(
        cleared={"a"}, offloaded={"b": OffloadRecord(preview="", chars=1)}
    )
    assert state.decided("a") and state.decided("b") and not state.decided("c")


# ---------------------------------------------------------------------------
# fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_stable_for_same_prefix():
    entries = [user("hi"), call("c1"), out("c1", "result")]
    assert fingerprint(entries) == fingerprint(list(entries))


def test_fingerprint_changes_when_prefix_changes():
    a = [user("hi"), call("c1"), out("c1", "result")]
    b = [user("hi"), call("c2"), out("c2", "result")]
    c = [user("hi!!"), call("c1"), out("c1", "result")]
    assert fingerprint(a) != fingerprint(b)
    assert fingerprint(a) != fingerprint(c)
    assert len(fingerprint(a)) == 16
