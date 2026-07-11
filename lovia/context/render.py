"""Pure view rendering: immutable transcript + sticky state → per-call view.

``render_view`` is deterministic and side-effect free. Untouched entries pass
through *by reference* (so :class:`~lovia.context.tokens.TokenCounter` memo
hits and identity comparisons keep working); only decided tool results are
replaced with marker entries and the summarized prefix with one summary entry.

Because the sticky state is monotonic, two consecutive renders differ only at
the decision frontier: everything before the oldest *new* decision is
byte-identical to the previous turn, which is what keeps provider prompt
caches warm.
"""

from __future__ import annotations

from .prompts import SUMMARY_WRAPPER
from .state import CompactionState, OffloadRecord
from .tokens import TokenCounter
from ..transcript import (
    InputEntry,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
    split_system,
)


def render_view(
    entries: list[TranscriptEntry],
    state: CompactionState,
) -> list[TranscriptEntry]:
    """Render the per-call view of ``entries`` under ``state``.

    Never mutates ``entries``. With an empty state this returns the same
    entry objects in a new list.
    """
    systems, body = split_system(entries)
    summary = state.summary
    if summary is not None and 0 < summary.covered <= len(body):
        rendered = [
            summary_entry(summary.text),
            *render_entries(body[summary.covered :], state),
        ]
    else:
        rendered = render_entries(body, state)
    return [*systems, *rendered]


def render_entries(
    entries: list[TranscriptEntry],
    state: CompactionState,
) -> list[TranscriptEntry]:
    """Apply clear/offload markers to ``entries``; pass the rest by reference."""
    out: list[TranscriptEntry] = []
    for entry in entries:
        if isinstance(entry, ToolResultEntry):
            record = state.offloaded.get(entry.call_id)
            if record is not None:
                out.append(
                    ToolResultEntry(
                        call_id=entry.call_id,
                        output=offload_marker(record),
                        raw=None,
                        is_error=entry.is_error,
                    )
                )
                continue
            if entry.call_id in state.cleared:
                out.append(
                    ToolResultEntry(
                        call_id=entry.call_id,
                        output=clear_marker(entry.call_id),
                        raw=None,
                        is_error=entry.is_error,
                    )
                )
                continue
        out.append(entry)
    return out


def summary_entry(text: str) -> InputEntry:
    """The user-role entry that carries the context summary in a view."""
    return InputEntry(role="user", content=SUMMARY_WRAPPER.format(summary=text))


def clear_marker(call_id: str) -> str:
    """Inline placeholder for a cleared tool result."""
    return (
        "[Earlier tool result cleared to save context. "
        f'Call recall_tool_result("{call_id}") to retrieve the full output.]'
    )


def offload_marker(record: OffloadRecord) -> str:
    """Inline placeholder for a large tool result trimmed to a preview.

    The recall reference is the record's content digest, not the call_id:
    the store is shared across sessions while call_ids are session-local
    (see :class:`~lovia.context.state.OffloadRecord.digest`). The model just
    echoes the reference back; the paired tool call right above the marker
    still tells it *which* call this was.
    """
    return (
        f"[Tool result ({record.chars:,} chars) trimmed to a preview to save context.\n"
        f"Preview:\n{record.preview}\n"
        f'Call recall_tool_result("{record.digest}") for the full output.]'
    )


def pair_safe_cuts(entries: list[TranscriptEntry]) -> list[bool]:
    """``safe[i]``: splitting ``entries`` at ``i`` severs no tool call/result pair.

    A cut with a call at ``a < i`` and its result at ``b >= i`` strands the
    result without its call — exactly what providers reject ("Messages with
    role 'tool' must be a response to a preceding message with 'tool_calls'").
    One flag per cut position (``len(entries) + 1``); for a well-formed
    transcript ``safe[0]`` and ``safe[len(entries)]`` are always ``True``.

    A result whose call appears nowhere in ``entries`` constrains nothing:
    that transcript was malformed before any cut, and refusing every split
    would only pin the pathology in place.

    Scanning backward, ``outstanding`` counts results already seen whose call
    has not yet been reached; a cut is safe exactly where that count is zero.
    Counting per id (not a set) keeps nested duplicates — ``call a, call a,
    out a, out a`` from an id-reusing provider — from reading as balanced too
    early.
    """
    call_ids = {e.call_id for e in entries if isinstance(e, ToolCallEntry)}
    safe = [True] * (len(entries) + 1)
    awaiting: dict[str, int] = {}
    outstanding = 0
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        if isinstance(entry, ToolResultEntry) and entry.call_id in call_ids:
            awaiting[entry.call_id] = awaiting.get(entry.call_id, 0) + 1
            outstanding += 1
        elif isinstance(entry, ToolCallEntry) and awaiting.get(entry.call_id):
            awaiting[entry.call_id] -= 1
            outstanding -= 1
        safe[i] = outstanding == 0
    return safe


def protected_tail_start(
    body: list[TranscriptEntry],
    counter: TokenCounter,
    ratio: float,
    tail_tokens: int,
) -> int:
    """Index of the first *protected* body entry.

    ``body[cut:]`` is the tail every stage must leave verbatim. The cut is
    chosen by walking backward until ``tail_tokens`` (calibrated) is filled,
    then adjusted:

    * the most recent entry is always protected;
    * the most recent user message is pulled into the tail when that keeps
      the tail under 2× its token budget (anchoring — the live request must
      not be summarized away; in long tool loops where the only user message
      is ancient, the summary's "Session intent" section carries it instead);
    * the cut expands leftward over tool call/result pairs so the tail never
      contains a result whose call would fall inside the compacted prefix.
    """
    n = len(body)
    if n == 0:
        return 0
    budget_raw = max(1, int(tail_tokens / max(ratio, 0.01)))

    acc = 0
    cut = n
    for i in range(n - 1, -1, -1):
        acc += counter.count_entry(body[i])
        if acc > budget_raw:
            break
        cut = i
    cut = min(cut, n - 1)

    last_user = next(
        (
            j
            for j in range(n - 1, -1, -1)
            if isinstance(body[j], InputEntry) and body[j].role == "user"  # type: ignore[union-attr]
        ),
        None,
    )
    if last_user is not None and last_user < cut:
        anchored = sum(counter.count_entry(e) for e in body[last_user:])
        if anchored <= 2 * budget_raw:
            cut = last_user

    # Snap leftward to a pair-safe cut so the tail never holds a result whose
    # call fell into the compacted prefix. ``safe[0]`` is the floor: a result
    # with no call anywhere is left as-is (already malformed).
    safe = pair_safe_cuts(body)
    while cut > 0 and not safe[cut]:
        cut -= 1
    return cut


__all__ = [
    "clear_marker",
    "offload_marker",
    "pair_safe_cuts",
    "protected_tail_start",
    "render_entries",
    "render_view",
    "summary_entry",
]
