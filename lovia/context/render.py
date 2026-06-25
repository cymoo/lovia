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
)


def split_system(
    entries: list[TranscriptEntry],
) -> tuple[list[TranscriptEntry], list[TranscriptEntry]]:
    """Split off the leading *run* of system messages; the rest is the *body*.

    Returns every consecutive leading ``system`` entry, not just the first. A
    caller may pass a ``system()`` input under a systemless agent, so after a
    handoff the transcript can briefly lead with two systems (the new agent's
    head + the caller's). Collapsing the whole leading run keeps the body — and
    thus the body-relative summary coverage — invariant when a handoff changes
    *how many* leading system entries there are, so the running summary survives
    instead of being reset. (Provider adapters merge all system messages into
    one ``system`` param regardless of count.)
    """
    n = 0
    for entry in entries:
        if isinstance(entry, InputEntry) and entry.role == "system":
            n += 1
        else:
            break
    return list(entries[:n]), list(entries[n:])


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
                        output=offload_marker(record, entry.call_id),
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


def offload_marker(record: OffloadRecord, call_id: str) -> str:
    """Inline placeholder for a tool result archived to the result store."""
    return (
        f"[Tool result ({record.chars:,} chars) archived to save context.\n"
        f"Preview:\n{record.preview}\n"
        f'Call recall_tool_result("{call_id}") for the full output.]'
    )


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

    # Expand leftward until no tool result in the tail is orphaned. Mirrors
    # ``safe_window``'s fixed-point loop but returns a pure index.
    while cut > 0:
        tail_calls = {e.call_id for e in body[cut:] if isinstance(e, ToolCallEntry)}
        orphans = {
            e.call_id
            for e in body[cut:]
            if isinstance(e, ToolResultEntry) and e.call_id not in tail_calls
        }
        if not orphans:
            break
        new_cut = cut
        for i in range(cut - 1, -1, -1):
            entry = body[i]
            if isinstance(entry, ToolCallEntry) and entry.call_id in orphans:
                new_cut = i
                orphans.discard(entry.call_id)
                if not orphans:
                    break
        if new_cut == cut:
            # Remaining orphans have no call anywhere earlier — the raw
            # transcript was already malformed; nothing more to protect.
            break
        cut = new_cut
    return cut


__all__ = [
    "clear_marker",
    "offload_marker",
    "protected_tail_start",
    "render_entries",
    "render_view",
    "split_system",
    "summary_entry",
]
