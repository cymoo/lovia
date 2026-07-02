"""Compaction stages: composable strategies that record sticky decisions.

A :class:`Stage` never edits the view. It inspects the transcript *body*
(system message stripped) plus the shared :class:`StageContext` and records
decisions into the sticky :class:`~lovia.context.state.CompactionState`; the
pipeline re-renders and re-counts after every stage that decided something.

The default order is cheap-first, mirroring Claude Code's /compact layering
and Anthropic's context-editing primitives:

1. :class:`OffloadToolResults` — replace huge results with a preview marker;
   when a store is configured, also archive the full output for durable recall
   (best-effort I/O).
2. :class:`ClearToolResults` — replace older tool results with tiny recall
   markers (free).
3. :class:`SummarizeHistory` — fold the older prefix into a running LLM
   summary (the only stage that costs inference; last resort).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .policy import CompactionRequest
from .render import clear_marker, offload_marker, render_entries
from .state import (
    CompactionState,
    OffloadRecord,
    SummaryState,
    fingerprint,
    unique_result_ids,
)
from .summarizer import LLMSummarizer, Summarizer
from .tokens import TokenBudget, TokenCounter
from ..transcript import ToolResultEntry, TranscriptEntry

if TYPE_CHECKING:
    from .store import ResultStore

logger = logging.getLogger(__name__)


@dataclass
class StageContext:
    """Shared facts a stage plans against.

    Attributes:
        request: The compaction request being served.
        state: Sticky decision state. Mutating this is a stage's only output
            channel besides its return value.
        counter: Token estimator (memoized).
        budget: Window watermarks for this call.
        current_tokens: Calibrated estimate of the current rendered view.
        protected_from: Body index of the first protected entry. Entries at
            or after it must stay verbatim for every stage.
        aggressive: ``True`` on the reactive overflow path — compact harder.
        store: Optional durable sink for offloaded result bodies. ``None`` does
            not disable :class:`OffloadToolResults` — it still emits preview
            markers and recall falls back to the transcript; a store keeps the
            output recoverable once the transcript no longer retains it.
        model_window: The model's real context window when known. On the
            aggressive path ``budget.window`` is derived from the *failed
            prompt* (which exceeded the real window), so anything that must
            fit in an actual model call — like the summarizer's fold chunks —
            sizes against this instead.
    """

    request: CompactionRequest
    state: CompactionState
    counter: TokenCounter
    budget: TokenBudget
    current_tokens: int
    protected_from: int
    aggressive: bool
    store: "ResultStore | None" = None
    model_window: int | None = None

    def calibrated(self, raw_tokens: int) -> int:
        """Apply the learned estimate→actual ratio to ``raw_tokens``."""
        return int(raw_tokens * self.state.ratio)


class Stage(Protocol):
    """One compaction strategy in a :class:`~lovia.context.Compaction`."""

    @property
    def name(self) -> str: ...

    async def plan(self, body: list[TranscriptEntry], ctx: StageContext) -> bool:
        """Record new sticky decisions in ``ctx.state``.

        ``body`` is the transcript with the leading system message stripped;
        it must not be mutated. Returns ``True`` when anything new was
        decided (the pipeline then re-renders and re-counts). A failing
        stage should log and return ``False`` rather than raise — on the
        overflow path an escaping exception replaces the original
        ``ContextOverflowError``; the pipeline still persists decisions made
        before an unexpected raise.
        """
        ...


def _result_indices(body: list[TranscriptEntry], state: CompactionState) -> list[int]:
    """Indices of the tool results a stage may still decide about.

    Two kinds are excluded. Results inside the summarized prefix: the summary
    already replaced them in the view, so a decision there would burn store
    I/O and sticky-state growth on entries that never render — and its
    projected token saving would be phantom, making the stage stop short of
    its target. And results whose ``call_id`` is not unique in the body
    (:func:`~lovia.context.state.unique_result_ids`): decisions are keyed by
    id, so a marker for a reused id would replace every occurrence,
    including the fresh result ``keep_last`` meant to protect.
    """
    covered = state.summary.covered if state.summary is not None else 0
    referable = unique_result_ids(body)
    return [
        i
        for i, e in enumerate(body)
        if i >= covered and isinstance(e, ToolResultEntry) and e.call_id in referable
    ]


def _oversized(entry: ToolResultEntry, ctx: StageContext) -> bool:
    """A result that single-handedly blows the target budget.

    On the aggressive (post-overflow) path such a result loses its
    keep-last/protected-tail immunity: keeping it verbatim guarantees the
    retry fails again, while a marker (with recall fallback) lets the
    run make progress.
    """
    return ctx.calibrated(ctx.counter.count_entry(entry)) > ctx.budget.target_tokens


class OffloadToolResults:
    """Replace large tool results with a short preview marker, oldest first.

    The view keeps a marker with a preview; the agent recovers the full content
    with ``recall_tool_result``, which reads the store first and falls back to
    the transcript. So this runs with or without a store — today the transcript
    still holds every output. A store earns its keep because that fallback is
    not guaranteed: a clearing policy may evict outputs from the transcript, and
    the store is the durable copy that outlives it. Archiving is best-effort —
    a failing store never blocks the marker.
    """

    name = "offload"

    def __init__(
        self,
        *,
        min_chars: int = 4_000,
        keep_last: int = 2,
        preview_chars: int = 400,
    ) -> None:
        """Configure offloading.

        Args:
            min_chars: Only results at least this long are offloaded.
            keep_last: The N most recent tool results are never offloaded.
            preview_chars: Length of the inline preview kept in the marker.
        """
        if min_chars < 1:
            raise ValueError("min_chars must be >= 1")
        if keep_last < 0:
            raise ValueError("keep_last must be >= 0")
        if preview_chars < 0:
            raise ValueError("preview_chars must be >= 0")
        self.min_chars = min_chars
        self.keep_last = keep_last
        self.preview_chars = preview_chars

    async def plan(self, body: list[TranscriptEntry], ctx: StageContext) -> bool:
        store = ctx.store
        result_idxs = _result_indices(body, ctx.state)
        keep_from = len(result_idxs) - self.keep_last
        tokens = ctx.current_tokens
        decided = False
        for pos, i in enumerate(result_idxs):
            if tokens <= ctx.budget.target_tokens:
                break
            entry = body[i]
            assert isinstance(entry, ToolResultEntry)
            protected = pos >= keep_from or i >= ctx.protected_from
            if protected and not (ctx.aggressive and _oversized(entry, ctx)):
                continue
            if len(entry.output) < self.min_chars or ctx.state.decided(entry.call_id):
                continue
            # Archiving is a best-effort side effect, decoupled from the
            # decision: recall falls back to the transcript, so a missing or
            # failing store never blocks the marker. The store still earns its
            # keep — the transcript holds every output today but isn't required
            # to forever (a clearing policy may evict them), and a durable store
            # is what survives that — so a failed put() is logged, not silent.
            if store is not None:
                try:
                    await store.put(entry.call_id, entry.output)
                except Exception as exc:
                    logger.warning(
                        "context.offload: store put for %s failed (%s: %s); "
                        "keeping marker, recall falls back to the transcript",
                        entry.call_id,
                        type(exc).__name__,
                        exc,
                    )
            record = OffloadRecord(
                preview=entry.output[: self.preview_chars],
                chars=len(entry.output),
            )
            ctx.state.offloaded[entry.call_id] = record
            marker_tokens = (
                len(offload_marker(record, entry.call_id)) // 4
                + ctx.counter.entry_overhead
            )
            saving = max(0, ctx.counter.count_entry(entry) - marker_tokens)
            tokens -= ctx.calibrated(saving)
            decided = True
        return decided


class ClearToolResults:
    """Replace older tool results with tiny recall markers.

    Follows the semantics of Anthropic's ``clear_tool_uses`` context edit:
    the most recent ``keep_last`` results survive, small results aren't worth
    a marker, and clearing proceeds oldest-first until the view is under the
    target watermark.
    """

    name = "clear"

    def __init__(
        self,
        *,
        keep_last: int = 3,
        min_chars: int = 200,
        clear_at_least_tokens: int | None = None,
    ) -> None:
        """Configure clearing.

        Args:
            keep_last: The N most recent tool results are never cleared
                (1 on the aggressive path).
            min_chars: Results at or below this length stay inline
                (0 on the aggressive path).
            clear_at_least_tokens: When set, keep clearing until at least
                this many (calibrated) tokens were freed even if the target
                watermark was already reached — amortizes the prompt-cache
                invalidation a new clearing burst causes.
        """
        if keep_last < 0:
            raise ValueError("keep_last must be >= 0")
        if min_chars < 0:
            raise ValueError("min_chars must be >= 0")
        self.keep_last = keep_last
        self.min_chars = min_chars
        self.clear_at_least_tokens = clear_at_least_tokens

    async def plan(self, body: list[TranscriptEntry], ctx: StageContext) -> bool:
        keep_last = 1 if ctx.aggressive else self.keep_last
        min_chars = 0 if ctx.aggressive else self.min_chars
        result_idxs = _result_indices(body, ctx.state)
        keep_from = len(result_idxs) - keep_last
        tokens = ctx.current_tokens
        freed = 0
        decided = False
        for pos, i in enumerate(result_idxs):
            if tokens <= ctx.budget.target_tokens and (
                self.clear_at_least_tokens is None
                or freed >= self.clear_at_least_tokens
            ):
                break
            entry = body[i]
            assert isinstance(entry, ToolResultEntry)
            protected = pos >= keep_from or i >= ctx.protected_from
            if protected and not (ctx.aggressive and _oversized(entry, ctx)):
                continue
            if len(entry.output) <= min_chars or ctx.state.decided(entry.call_id):
                continue
            ctx.state.cleared.add(entry.call_id)
            marker_tokens = (
                len(clear_marker(entry.call_id)) // 4 + ctx.counter.entry_overhead
            )
            saving = ctx.calibrated(
                max(0, ctx.counter.count_entry(entry) - marker_tokens)
            )
            tokens -= saving
            freed += saving
            decided = True
        return decided


class SummarizeHistory:
    """Fold the unprotected prefix into a running LLM summary.

    The summary is incremental: only the span between the previous coverage
    frontier and the protected tail is sent to the summarizer, together with
    the prior summary text — in chunks bounded by half the usable window, so
    the summary call itself cannot overflow even when a whole long prefix
    must be (re)covered at once. Between bursts the existing summary is
    replayed verbatim by the renderer at zero cost.

    Coverage is bounded only by the pipeline's protected token tail
    (``keep_recent_tokens``), not by other stages' ``keep_last``: a result
    recent enough to escape clearing can still be folded into the summary
    once it leaves the tail — its ``call_id`` stays recallable via the
    summary's Artifacts section.
    """

    name = "summary"

    def __init__(
        self,
        *,
        summarizer: Summarizer | None = None,
        min_savings_ratio: float = 0.10,
        max_failures: int = 3,
        max_summary_chars: int | None = 16_000,
    ) -> None:
        """Configure summarization.

        Args:
            summarizer: Summary backend. Defaults to :class:`LLMSummarizer`
                using the run's own provider.
            min_savings_ratio: Skip the (expensive) summary call when the
                projected saving is below this fraction of the current view —
                anti-thrash. Ignored on the aggressive path.
            max_failures: Consecutive summarizer failures before the circuit
                breaker stops trying proactively. The aggressive (overflow)
                path still attempts — it is the half-open probe that lets a
                session recover once the summarizer works again.
            max_summary_chars: Reject a summary longer than this (treated as a
                failure). The summary is replayed verbatim into every view, so
                this is a safety valve against a misbehaving summarizer growing
                it without bound: ~16k chars is ~4k tokens of fixed overhead
                per call — well past the point where a "summary" stops being
                compression (the prompt asks for under 2000 words).
                ``None`` disables the cap.
        """
        if not 0 <= min_savings_ratio < 1:
            raise ValueError("min_savings_ratio must be in [0, 1)")
        if max_failures < 1:
            raise ValueError("max_failures must be >= 1")
        if max_summary_chars is not None and max_summary_chars < 1:
            raise ValueError("max_summary_chars must be >= 1")
        self.summarizer: Summarizer = summarizer or LLMSummarizer()
        self.min_savings_ratio = min_savings_ratio
        self.max_failures = max_failures
        self.max_summary_chars = max_summary_chars

    async def plan(self, body: list[TranscriptEntry], ctx: StageContext) -> bool:
        state = ctx.state
        # Circuit breaker with a half-open probe: proactive attempts stop
        # after ``max_failures``, but the aggressive path still tries — the
        # failure counter is carried in the scratch across runs like every
        # other sticky decision, so without a probe a burst of transient
        # failures would disable summarization for the rest of the session.
        if state.summary_failures >= self.max_failures and not ctx.aggressive:
            logger.warning(
                "context.summary: circuit breaker tripped after %d failures",
                state.summary_failures,
            )
            return False

        prior = state.summary
        prior_covered = prior.covered if prior is not None else 0
        new_covered = ctx.protected_from
        if new_covered <= prior_covered:
            return False

        # The summarizer sees the *rendered* span: cleared/offloaded results
        # appear as markers, so file paths land in the summary's Artifacts
        # section instead of megabytes of content.
        span = render_entries(body[prior_covered:new_covered], state)
        span_tokens = ctx.calibrated(ctx.counter.count(span))
        growth = max(256, len(prior.text) // 4 if prior is not None else 512)
        projected_savings = span_tokens - growth
        if (
            not ctx.aggressive
            and projected_savings < self.min_savings_ratio * ctx.current_tokens
        ):
            return False

        # Fold in chunks bounded by half the usable window: the summary call
        # itself goes through a model (by default the run's own), so an
        # unbounded span — e.g. re-summarizing a long prefix after a summary
        # reset — would overflow the very window this stage exists to protect.
        # Each successful fold is committed to the sticky state immediately,
        # so a failure mid-way keeps the coverage already gained.
        usable = ctx.budget.usable
        if ctx.model_window is not None:
            # The aggressive budget is sized to the failed prompt, which can
            # dwarf the real window; the fold chunks must fit the model.
            usable = min(usable, max(1, ctx.model_window - ctx.budget.reserve_output))
        cap = max(1, usable // 2)
        running = prior.text if prior is not None else None
        folded = False
        start = 0
        while start < len(span):
            end = start
            acc = 0
            while end < len(span):
                tokens = ctx.calibrated(ctx.counter.count_entry(span[end]))
                # A single entry above the cap still gets its own chunk —
                # the cap is conservative, so the fold may well fit anyway.
                if end > start and acc + tokens > cap:
                    break
                acc += tokens
                end += 1

            try:
                text = await self.summarizer.summarize(
                    span[start:end], req=ctx.request, prior_summary=running
                )
            except Exception as exc:
                # Never propagate, even on the aggressive path: replacing the
                # original ContextOverflowError with the summarizer's own
                # failure would misattribute the run's death. Returning what
                # was folded so far lets the pipeline report honestly and the
                # runner surface the real overflow.
                state.summary_failures += 1
                logger.warning(
                    "context.summary: summarizer failed (%s: %s); failure %d/%d",
                    type(exc).__name__,
                    exc,
                    state.summary_failures,
                    self.max_failures,
                )
                return folded

            # Guard against a misbehaving (usually custom) summarizer: an
            # empty summary would silently blank the covered prefix, and an
            # over-long one would bloat every future view (it's replayed
            # verbatim). Reject either like a failure — don't extend
            # coverage; the circuit breaker stops retries, and the prefix
            # stays for clear/offload or a surfaced overflow.
            if not text.strip() or (
                self.max_summary_chars is not None
                and len(text) > self.max_summary_chars
            ):
                state.summary_failures += 1
                logger.warning(
                    "context.summary: rejected summary (chars=%d, empty=%s); "
                    "failure %d/%d",
                    len(text),
                    not text.strip(),
                    state.summary_failures,
                    self.max_failures,
                )
                return folded

            state.summary_failures = 0
            covered = prior_covered + end
            state.summary = SummaryState(
                text=text,
                covered=covered,
                fingerprint=fingerprint(body[:covered]),
            )
            running = text
            folded = True
            start = end
        return folded


__all__ = [
    "ClearToolResults",
    "OffloadToolResults",
    "Stage",
    "StageContext",
    "SummarizeHistory",
]
