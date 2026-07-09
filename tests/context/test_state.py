"""Tests for sticky compaction state (de)serialization and fingerprints."""

from __future__ import annotations

import json

from lovia.context import CompactionState, OffloadRecord, SummaryState
from lovia.context.state import fingerprint, unique_result_ids, window_key
from .helpers import call, out, user


def _full_state() -> CompactionState:
    return CompactionState(
        cleared={"c1", "c2"},
        offloaded={"c3": OffloadRecord(preview="pre", chars=9000)},
        summary=SummaryState(text="S", covered=4, fingerprint="ab" * 8),
        ratio=1.5,
        last_view_estimate=1234,
        summary_failures=1,
        learned_windows={"https://api.deepseek.com/v1\x00deepseek-chat": 65_536},
    )


def test_state_round_trips_through_scratch():
    scratch: dict = {}
    _full_state().save(scratch)
    loaded = CompactionState.load(scratch)
    assert loaded == _full_state()


def test_state_round_trips_through_checkpoint_json():
    """Exact checkpoint path: context_state serialized as JSON and back."""
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


def test_load_drops_malformed_learned_windows():
    raw = {
        "version": 2,
        "learned_windows": {
            "ok\x00m": 65_536,
            "negative\x00m": -1,
            "not-an-int\x00m": "65536",
            "bool\x00m": True,
            7: 4096,
        },
    }
    state = CompactionState.load({"context": raw})
    assert state.learned_windows == {"ok\x00m": 65_536}


def test_scratch_without_learned_windows_keeps_its_other_decisions():
    """Adding the key must not have bumped ``_VERSION``.

    A version bump silently discards every sticky decision a user's session
    already carries; scratch written before this field must still load.
    """
    old = {
        "context": {
            "version": 2,
            "cleared": ["c1"],
            "offloaded": {"c3": {"preview": "pre", "chars": 9000}},
            "summary": {"text": "S", "covered": 4, "fingerprint": "ab" * 8},
            "ratio": 1.5,
            "last_view_estimate": 1234,
            "summary_failures": 1,
        }
    }
    state = CompactionState.load(old)
    assert state.cleared == {"c1"}
    assert state.offloaded == {"c3": OffloadRecord(preview="pre", chars=9000)}
    assert state.summary == SummaryState(text="S", covered=4, fingerprint="ab" * 8)
    assert state.learned_windows == {}


def test_window_key_separates_endpoints_and_tolerates_a_missing_base_url():
    class _Provider:
        base_url = "https://a.test/v1"

    class _Bare:
        pass

    assert window_key(_Provider(), "m") != window_key(_Bare(), "m")
    assert window_key(_Bare(), "m") == "\x00m"
    assert window_key(_Provider(), None) == "https://a.test/v1\x00"


def test_decided_covers_both_kinds():
    state = CompactionState(
        cleared={"a"}, offloaded={"b": OffloadRecord(preview="", chars=1)}
    )
    assert state.decided("a") and state.decided("b") and not state.decided("c")


# ---------------------------------------------------------------------------
# unique_result_ids / prune
# ---------------------------------------------------------------------------


def test_unique_result_ids_excludes_reused_ids():
    body = [
        call("a"),
        out("a", "x"),
        call("call_0"),
        out("call_0", "x"),
        call("call_0"),
        out("call_0", "y"),  # provider reused the id
    ]
    assert unique_result_ids(body) == {"a"}


def test_prune_drops_absent_and_ambiguous_records():
    state = CompactionState(
        cleared={"gone", "dup", "live"},
        offloaded={
            "gone2": OffloadRecord(preview="p", chars=9),
            "live2": OffloadRecord(preview="p", chars=9),
        },
    )
    state.prune({"live", "live2"})
    assert state.cleared == {"live"}
    assert set(state.offloaded) == {"live2"}


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


def test_fingerprint_ignores_tool_result_length():
    # A stored tool output trimmed in place (session cleanup) must not read
    # as a rewrite — the summary covers a marker, not the output itself.
    a = [user("hi"), call("c1"), out("c1", "x" * 10_000)]
    b = [user("hi"), call("c1"), out("c1", "x" * 10)]
    assert fingerprint(a) == fingerprint(b)
