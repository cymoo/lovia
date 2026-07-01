"""Sticky compaction state: the decisions a pipeline has already made.

This is the heart of the *plan/render split*: stages never edit the view —
they record decisions here, and :func:`~lovia.context.render.render_view`
deterministically rebuilds the per-call view from the immutable transcript
plus this state. Decisions are **monotonic** (a cleared tool result never
reverts; summary coverage only grows), so the rendered prompt prefix is
byte-stable across turns — which is exactly what provider prompt caches need.

The state lives inside the runner-owned per-run ``scratch`` dict
(:attr:`~lovia.checkpointer.RunSnapshot.context_state`) as plain
JSON types, so it survives checkpoint/resume for free, and the runner carries
it to the next run on the same session (via the segment ``meta``) so decisions
persist without re-deriving. :meth:`CompactionState.load` is deliberately
forgiving: garbage, missing keys, or a different schema version simply mean a
fresh state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..parts import TextPart
from ..transcript import (
    AssistantTextEntry,
    InputEntry,
    ReasoningEntry,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
)

SCRATCH_KEY = "context"
"""Key under which compaction state lives inside the per-run scratch dict."""
_VERSION = 2


@dataclass
class OffloadRecord:
    """A large tool result replaced by a preview marker in the view, keyed by
    call_id (archived to the result store too, when one is configured)."""

    preview: str
    """The first characters of the output, kept inline as a teaser."""
    chars: int
    """Length of the original output."""


@dataclass
class SummaryState:
    """The running summary and exactly which transcript prefix it replaces."""

    text: str
    """Current summary text, replayed verbatim until coverage extends."""
    covered: int
    """Number of leading *body* entries (transcript minus the leading system
    message) the summary replaces."""
    fingerprint: str
    """Digest of the covered prefix (:func:`fingerprint`). A mismatch means the
    covered prefix changed under us — e.g. the summary was carried into a new
    run whose session history was trimmed or rewritten — so it no longer
    describes what it claims to cover."""


@dataclass
class CompactionState:
    """Everything a :class:`~lovia.context.Compaction` has decided so far.

    Attributes:
        cleared: ``call_id``\\ s whose tool results render as a tiny recall
            marker.
        offloaded: ``call_id`` → :class:`OffloadRecord` for results replaced by
            a preview marker (and archived to the store, when one is configured).
        summary: The running summary, or ``None`` before the first one.
        ratio: Calibration multiplier mapping heuristic token estimates to
            the provider's real input-token counts (EMA, clamped).
        last_view_estimate: Raw (uncalibrated) estimate of the view returned
            by the previous ``compact()`` call; compared against the next
            real ``last_input_tokens`` to update :attr:`ratio`.
        summary_failures: Consecutive summarizer failures this run; the
            summarize stage stops trying after its limit (circuit breaker).
    """

    cleared: set[str] = field(default_factory=set)
    offloaded: dict[str, OffloadRecord] = field(default_factory=dict)
    summary: SummaryState | None = None
    ratio: float = 1.0
    last_view_estimate: int | None = None
    summary_failures: int = 0

    def decided(self, call_id: str) -> bool:
        """Whether a sticky decision already exists for this tool result."""
        return call_id in self.cleared or call_id in self.offloaded

    @classmethod
    def load(cls, scratch: dict[str, Any]) -> "CompactionState":
        """Rebuild from ``scratch``, tolerating missing or malformed data."""
        state = cls()
        raw = scratch.get(SCRATCH_KEY)
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            return state

        cleared = raw.get("cleared")
        if isinstance(cleared, (list, set)):
            state.cleared = {c for c in cleared if isinstance(c, str)}

        offloaded = raw.get("offloaded")
        if isinstance(offloaded, dict):
            for call_id, rec in offloaded.items():
                if (
                    isinstance(call_id, str)
                    and isinstance(rec, dict)
                    and isinstance(rec.get("preview"), str)
                    and isinstance(rec.get("chars"), int)
                ):
                    state.offloaded[call_id] = OffloadRecord(
                        preview=rec["preview"], chars=rec["chars"]
                    )

        summary = raw.get("summary")
        if (
            isinstance(summary, dict)
            and isinstance(summary.get("text"), str)
            and isinstance(summary.get("covered"), int)
            and summary["covered"] > 0
            and isinstance(summary.get("fingerprint"), str)
        ):
            state.summary = SummaryState(
                text=summary["text"],
                covered=summary["covered"],
                fingerprint=summary["fingerprint"],
            )

        ratio = raw.get("ratio")
        if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
            state.ratio = min(4.0, max(0.5, float(ratio)))

        estimate = raw.get("last_view_estimate")
        if isinstance(estimate, int) and not isinstance(estimate, bool):
            state.last_view_estimate = estimate

        failures = raw.get("summary_failures")
        if isinstance(failures, int) and not isinstance(failures, bool):
            state.summary_failures = failures

        return state

    def save(self, scratch: dict[str, Any]) -> None:
        """Serialize into ``scratch`` as plain JSON types."""
        scratch[SCRATCH_KEY] = {
            "version": _VERSION,
            "cleared": sorted(self.cleared),
            "offloaded": {
                call_id: {"preview": r.preview, "chars": r.chars}
                for call_id, r in self.offloaded.items()
            },
            "summary": (
                {
                    "text": self.summary.text,
                    "covered": self.summary.covered,
                    "fingerprint": self.summary.fingerprint,
                }
                if self.summary is not None
                else None
            ),
            "ratio": self.ratio,
            "last_view_estimate": self.last_view_estimate,
            "summary_failures": self.summary_failures,
        }


def fingerprint(entries: Sequence[TranscriptEntry]) -> str:
    """Cheap structural digest of a transcript prefix.

    Captures entry kinds, call ids/roles, and content lengths — enough to
    detect the covered prefix being rewritten out from under a summary (e.g.
    when a summary is carried into a new run), without hashing megabytes of
    content.
    """
    h = hashlib.sha1()
    for entry in entries:
        h.update(_signature(entry).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _signature(entry: TranscriptEntry) -> str:
    if isinstance(entry, InputEntry):
        if isinstance(entry.content, str):
            size = len(entry.content)
        else:
            size = sum(
                len(p.text) if isinstance(p, TextPart) else 1 for p in entry.content
            )
        return f"input:{entry.role}:{size}"
    if isinstance(entry, AssistantTextEntry):
        return f"assistant:{len(entry.content)}"
    if isinstance(entry, ReasoningEntry):
        return f"reasoning:{len(entry.content)}"
    if isinstance(entry, ToolCallEntry):
        return f"call:{entry.call_id}:{len(entry.arguments)}"
    if isinstance(entry, ToolResultEntry):
        return f"result:{entry.call_id}:{len(entry.output)}"
    return f"unknown:{type(entry).__name__}"  # pragma: no cover - exhaustive


__all__ = ["CompactionState", "OffloadRecord", "SummaryState", "fingerprint"]
