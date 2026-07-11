"""Tests for pure view rendering and the protected-tail boundary."""

from __future__ import annotations

from lovia.context import (
    CompactionState,
    OffloadRecord,
    SummaryState,
    TokenCounter,
    render_view,
)
from lovia.context.render import pair_safe_cuts, protected_tail_start
from lovia.context.state import fingerprint
from lovia.transcript import (
    AssistantTextEntry,
    InputEntry,
    ToolResultEntry,
    entry_to_dict,
    leading_system_count,
    split_system,
)

from .helpers import call, out, system, user


def _assistant(s: str) -> AssistantTextEntry:
    return AssistantTextEntry(content=s)


# ---------------------------------------------------------------------------
# split_system / render_view
# ---------------------------------------------------------------------------


def test_split_system():
    sys0, user0 = system("sys"), user("hi")
    systems, body = split_system([sys0, user0])
    assert systems == [sys0]
    assert body == [user0]
    # No leading system -> empty system run.
    systems2, body2 = split_system([user0])
    assert systems2 == [] and body2 == [user0]
    # The whole leading run is collected, not just the first (e.g. a handoff that
    # leaves the new agent's head in front of a caller-supplied system input).
    a, b = system("a"), system("b")
    systems3, body3 = split_system([a, b, user0])
    assert systems3 == [a, b] and body3 == [user0]


def test_leading_system_count():
    u = user("hi")
    assert leading_system_count([]) == 0
    assert leading_system_count([u]) == 0
    assert leading_system_count([system("a"), system("b"), u]) == 2
    # Stops at the first non-system; a later system does not count.
    assert leading_system_count([system("a"), u, system("b")]) == 1


def test_render_with_empty_state_passes_entries_through_by_reference():
    entries = [system("sys"), user("hi"), call("c1"), out("c1", "r")]
    view = render_view(entries, CompactionState())
    assert view is not entries
    assert all(a is b for a, b in zip(view, entries))


def test_render_is_pure_and_deterministic():
    entries = [system("sys"), call("c1"), out("c1", "x" * 500), user("hi")]
    state = CompactionState(cleared={"c1"})
    a = [entry_to_dict(e) for e in render_view(entries, state)]
    b = [entry_to_dict(e) for e in render_view(entries, state)]
    assert a == b
    # The transcript itself was never touched.
    assert entries[2].output == "x" * 500


def test_cleared_marker_preserves_call_id_and_error_flag():
    entries = [
        call("c1"),
        ToolResultEntry(call_id="c1", output="x" * 500, is_error=True),
    ]
    view = render_view(entries, CompactionState(cleared={"c1"}))
    marker = view[1]
    assert isinstance(marker, ToolResultEntry)
    assert marker.call_id == "c1"
    assert marker.is_error is True
    assert marker.raw is None
    assert 'recall_tool_result("c1")' in marker.output


def test_offload_marker_mentions_preview_and_recall():
    record = OffloadRecord(preview="first lines", chars=9000, digest="ab12" * 4)
    entries = [call("c1"), out("c1", "x" * 9000)]
    view = render_view(entries, CompactionState(offloaded={"c1": record}))
    marker = view[1]
    assert "first lines" in marker.output
    # The recall reference is the content digest, not the session-local
    # call_id — the store is shared across sessions.
    assert f'recall_tool_result("{"ab12" * 4}")' in marker.output
    assert 'recall_tool_result("c1")' not in marker.output
    assert "9,000" in marker.output


def test_summary_replaces_covered_prefix_after_system():
    body = [user("u0"), _assistant("a0"), user("u1"), _assistant("a1")]
    entries = [system("sys"), *body]
    state = CompactionState(
        summary=SummaryState(
            text="THE SUMMARY", covered=2, fingerprint=fingerprint(body[:2])
        )
    )
    view = render_view(entries, state)
    assert view[0] is entries[0]  # system kept
    assert isinstance(view[1], InputEntry) and view[1].role == "user"
    assert "THE SUMMARY" in view[1].content
    assert view[2:] == body[2:]


def test_summary_wrapper_frames_as_background_reference():
    state = CompactionState(summary=SummaryState(text="S", covered=1, fingerprint="x"))
    view = render_view([user("u0"), user("u1")], state)
    text = view[0].content
    assert "<context_summary>" in text
    assert "NOT a new instruction" in text


def test_out_of_range_summary_coverage_is_ignored():
    state = CompactionState(summary=SummaryState(text="S", covered=10, fingerprint="x"))
    entries = [user("only")]
    view = render_view(entries, state)
    assert view == entries


# ---------------------------------------------------------------------------
# protected_tail_start
# ---------------------------------------------------------------------------


def test_tail_cut_by_token_budget():
    body = [user("x" * 100) for _ in range(10)]  # 33 tokens each
    cut = protected_tail_start(body, TokenCounter(), 1.0, tail_tokens=70)
    assert cut == 8  # two entries fit in 70 tokens


def test_tail_always_protects_most_recent_entry():
    body = [user("x" * 1000)]  # 258 tokens, way over budget
    assert protected_tail_start(body, TokenCounter(), 1.0, tail_tokens=10) == 0


def test_tail_anchors_last_user_message_when_affordable():
    body = [user("hi"), _assistant("a" * 4), _assistant("a" * 4), _assistant("a" * 4)]
    cut = protected_tail_start(body, TokenCounter(), 1.0, tail_tokens=20)
    assert cut == 0  # pulled back to the user message (35 tokens <= 2*20)


def test_tail_skips_anchor_when_too_expensive():
    body = [user("x" * 200)] + [_assistant("a" * 40) for _ in range(5)]
    cut = protected_tail_start(body, TokenCounter(), 1.0, tail_tokens=20)
    assert cut == 5  # anchoring would cost 148 tokens > 2*20


def test_tail_expands_over_tool_pairs():
    # The user message is too big to anchor, so the cut lands on the result —
    # which must drag its tool call into the tail.
    body = [user("x" * 600), call("c1"), out("c1", "r" * 200)]
    cut = protected_tail_start(body, TokenCounter(), 1.0, tail_tokens=60)
    assert cut == 1


def test_tail_ratio_shrinks_raw_budget():
    body = [user("x" * 100) for _ in range(10)]
    # ratio 2.0 → raw budget halves → only one entry fits.
    cut = protected_tail_start(body, TokenCounter(), 2.0, tail_tokens=70)
    assert cut == 9


def test_tail_empty_body():
    assert protected_tail_start([], TokenCounter(), 1.0, tail_tokens=100) == 0


def test_render_duplicate_call_ids_clears_every_result():
    entries = [call("c1"), out("c1", "x" * 500), out("c1", "y" * 500)]
    view = render_view(entries, CompactionState(cleared={"c1"}))
    assert all(
        "cleared to save context" in e.output
        for e in view
        if isinstance(e, ToolResultEntry)
    )


# ---------------------------------------------------------------------------
# pair_safe_cuts
# ---------------------------------------------------------------------------


def test_pair_safe_cuts_marks_inside_of_each_pair():
    body = [user("q"), call("c1"), out("c1"), call("c2"), out("c2")]
    assert pair_safe_cuts(body) == [True, True, False, True, False, True]


def test_pair_safe_cuts_parallel_calls_block_the_whole_group():
    # [call A, call B, result A, result B]: any cut inside the group strands
    # a result from one of the calls.
    body = [call("a"), call("b"), out("a"), out("b")]
    assert pair_safe_cuts(body) == [True, False, False, False, True]


def test_pair_safe_cuts_duplicate_ids_pair_like_parentheses():
    body = [call("a"), out("a"), call("a"), out("a")]
    assert pair_safe_cuts(body) == [True, False, True, False, True]


def test_pair_safe_cuts_orphan_result_constrains_nothing():
    # A result with no earlier call was malformed before any cut.
    body = [out("ghost"), user("q")]
    assert pair_safe_cuts(body) == [True, True, True]


def test_pair_safe_cuts_unresolved_call_constrains_nothing():
    # A call with no result strands nothing when cut away.
    body = [user("q"), call("pending")]
    assert pair_safe_cuts(body) == [True, True, True]


def test_pair_safe_cuts_nested_duplicate_ids():
    # An id-reusing provider can nest same-id pairs: [call a, call a, out a,
    # out a]. A set would read the group as balanced after one call and mark
    # an interior cut safe; the per-id counter keeps the whole group closed.
    body = [call("a"), call("a"), out("a"), out("a")]
    assert pair_safe_cuts(body) == [True, False, False, False, True]


def test_pair_safe_cuts_matches_provider_truth_by_brute_force():
    # Cross-check against the ground truth a provider enforces: a cut is unsafe
    # iff the tail holds a result whose matching call landed in the head.
    from collections import Counter as _Counter
    from itertools import product

    from lovia.transcript import ToolCallEntry

    def naive(entries: list) -> list[bool]:
        call_ids = {e.call_id for e in entries if isinstance(e, ToolCallEntry)}
        flags = []
        for i in range(len(entries) + 1):
            open_calls: _Counter = _Counter()
            severed = False
            for e in entries[i:]:
                if isinstance(e, ToolCallEntry):
                    open_calls[e.call_id] += 1
                elif isinstance(e, ToolResultEntry):
                    if open_calls[e.call_id] > 0:
                        open_calls[e.call_id] -= 1
                    elif e.call_id in call_ids:  # its call is in the head
                        severed = True
                        break
            flags.append(not severed)
        return flags

    # Every sequence of up to 6 tokens drawn from a 2-id alphabet of
    # calls/results/plain-user entries — covers nesting, interleaving,
    # duplicates, orphans, and malformed orderings.
    alphabet = [call("a"), call("b"), out("a"), out("b"), user("u")]
    for length in range(5):
        for combo in product(alphabet, repeat=length):
            seq = list(combo)
            assert pair_safe_cuts(seq) == naive(seq), seq


def test_pair_safe_cuts_empty():
    assert pair_safe_cuts([]) == [True]


# ---------------------------------------------------------------------------
# Review round: the protected-tail anchoring rule (negative side)
# ---------------------------------------------------------------------------


def test_tail_anchor_is_skipped_when_too_expensive():
    """The anchor rule only reaches back to the last user message while the
    whole stretch stays under 2x the tail budget; an ancient/huge user
    message is left to the summary's "Session intent" section instead."""
    body = [user("u" * 6_000), call("a"), out("a", "x" * 400), out("b", "x" * 400)]
    assert protected_tail_start(body, TokenCounter(), 1.0, 150) > 0
