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
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..parts import TextPart
from ..providers._windows import plausible_window
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

RATIO_MIN, RATIO_MAX = 0.5, 4.0
"""Clamp bounds for the calibration ratio, applied both when updating the EMA
and when loading persisted state — one weird usage report (or a corrupted
scratch) must not poison the estimate scale."""


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
            the provider's real input-token counts (EMA, clamped). The
            estimate it multiplies already includes the tool-schema overhead,
            so the ratio only absorbs tokenizer error, not additive payload.
        last_view_estimate: Raw (uncalibrated) estimate of the view returned
            by the previous ``compact()`` call, tool-schema overhead
            included; compared against the next real ``last_input_tokens``
            to update :attr:`ratio`.
        summary_failures: Consecutive summarizer failures, carried in the
            scratch like every other decision. Past the summarize stage's
            limit the proactive path stops trying (circuit breaker); the
            aggressive path still probes, and a success resets the count.
        learned_windows: Context windows the endpoint named in its own overflow
            rejections, keyed by :func:`window_key`. Persisting them is the
            point: an unknown or overstated window costs one overflow, once,
            and every later run on this session sizes itself correctly.
    """

    cleared: set[str] = field(default_factory=set)
    offloaded: dict[str, OffloadRecord] = field(default_factory=dict)
    summary: SummaryState | None = None
    ratio: float = 1.0
    last_view_estimate: int | None = None
    summary_failures: int = 0
    learned_windows: dict[str, int] = field(default_factory=dict)

    def decided(self, call_id: str) -> bool:
        """Whether a sticky decision already exists for this tool result."""
        return call_id in self.cleared or call_id in self.offloaded

    def prune(self, referable: set[str]) -> None:
        """Drop clear/offload records whose id is not in ``referable``.

        ``referable`` is :func:`unique_result_ids` of the current body: a
        record whose id is absent points at a result the session history no
        longer contains (trimmed or rewritten between runs), and one whose id
        is now duplicated would render *every* matching result as a marker —
        including a fresh one the decision was never about. Either way the
        record is stale; dropping it merely returns those results to verbatim
        rendering. Records for summary-covered entries survive (their ids are
        still unique in the body), which matters because a summary reset must
        find them intact.
        """
        self.cleared &= referable
        self.offloaded = {
            call_id: rec
            for call_id, rec in self.offloaded.items()
            if call_id in referable
        }

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
            state.ratio = min(RATIO_MAX, max(RATIO_MIN, float(ratio)))

        estimate = raw.get("last_view_estimate")
        if isinstance(estimate, int) and not isinstance(estimate, bool):
            state.last_view_estimate = estimate

        failures = raw.get("summary_failures")
        if isinstance(failures, int) and not isinstance(failures, bool):
            state.summary_failures = failures

        learned = raw.get("learned_windows")
        if isinstance(learned, dict):
            state.learned_windows = {
                key: value
                for key, value in learned.items()
                if isinstance(key, str) and plausible_window(value)
            }

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
            "learned_windows": dict(self.learned_windows),
        }


def window_key(provider: object, model: str | None) -> str:
    """Identity of a learned context window: the endpoint *and* the model.

    The same model name is served at different limits by different hosts (a
    vLLM box started with ``--max-model-len``, a gateway capping a shared
    model), so ``base_url`` is part of the key. Providers without one — test
    doubles, custom adapters — collapse to an empty prefix, which is right:
    they only ever speak to a single endpoint.
    """
    base_url = getattr(provider, "base_url", "") or ""
    return f"{base_url}\x00{model or ''}"


def unique_result_ids(entries: Sequence[TranscriptEntry]) -> set[str]:
    """``call_id``\\ s carried by exactly one tool result in ``entries``.

    These are the only ids a sticky decision (or a recall marker) can
    reference unambiguously. Providers with globally unique ids put every
    result here; providers that reuse ids per turn (``call_0``, ``call_1``)
    make repeated ids ambiguous, and the pipeline neither decides about nor
    replays decisions for those.
    """
    counts = Counter(e.call_id for e in entries if isinstance(e, ToolResultEntry))
    return {call_id for call_id, n in counts.items() if n == 1}


def fingerprint(entries: Sequence[TranscriptEntry]) -> str:
    """Cheap structural digest of a transcript prefix.

    Captures entry kinds, call ids/roles, and content lengths — enough to
    detect the covered prefix being rewritten out from under a summary (e.g.
    when a summary is carried into a new run), without hashing megabytes of
    content. Tool-result *lengths* are deliberately excluded: the summary
    covers markers, not the outputs themselves, so trimming a stored output
    in place (a session cleanup) must not read as a rewrite.
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
        return f"result:{entry.call_id}"
    return f"unknown:{type(entry).__name__}"  # pragma: no cover - exhaustive


__all__ = [
    "CompactionState",
    "OffloadRecord",
    "SummaryState",
    "fingerprint",
    "unique_result_ids",
]
