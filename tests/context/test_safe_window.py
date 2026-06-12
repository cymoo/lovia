"""Tests for pair-aware transcript slicing (``safe_window``)."""

from __future__ import annotations

from lovia import InputEntry, ToolCallEntry, ToolResultEntry, safe_window


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


def test_safe_window_negative_head_treated_as_zero():
    """A negative head must not slice from the end of the list."""
    entries = [_user(f"m{i}") for i in range(5)]
    assert safe_window(entries, head=-1, tail=0) == []
    got = safe_window(entries, head=-1, tail=2)
    assert [it.content for it in got] == ["m3", "m4"]


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
    """head=0 is how the context pipeline calls safe_window."""
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
