"""Compaction stages: composable strategies that record sticky decisions.

A :class:`Stage` never edits the view. It inspects the transcript *body*
(system message stripped) plus the shared :class:`StageContext` and records
decisions into the sticky :class:`~lovia.context.state.CompactionState`; the
pipeline re-renders and re-counts after every stage that decided something.

The default order is cheap-first, mirroring Claude Code's /compact layering
and Anthropic's context-editing primitives:

1. :class:`OffloadToolResults` — archive huge results to workspace files
   (I/O only; inert without a workspace).
2. :class:`ClearToolResults` — replace older tool results with tiny recall
   markers (free).
3. :class:`SummarizeHistory` — fold the older prefix into a running LLM
   summary (the only stage that costs inference; last resort).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from .policy import CompactionRequest
from .render import clear_marker, offload_marker, render_entries
from .state import CompactionState, OffloadRecord, SummaryState, fingerprint
from .summarizer import LLMSummarizer, Summarizer
from .tokens import TokenBudget, TokenCounter
from ..transcript import ToolCallEntry, ToolResultEntry, TranscriptEntry

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
    """

    request: CompactionRequest
    state: CompactionState
    counter: TokenCounter
    budget: TokenBudget
    current_tokens: int
    protected_from: int
    aggressive: bool

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
        decided (the pipeline then re-renders and re-counts). May raise only
        on the aggressive path, where the caller propagates failures.
        """
        ...


def _tool_names(body: list[TranscriptEntry]) -> dict[str, str]:
    """Map ``call_id`` → tool name so stages can honor ``exclude_tools``."""
    return {
        entry.call_id: entry.name for entry in body if isinstance(entry, ToolCallEntry)
    }


def _result_indices(body: list[TranscriptEntry]) -> list[int]:
    return [i for i, e in enumerate(body) if isinstance(e, ToolResultEntry)]


def _oversized(entry: ToolResultEntry, ctx: StageContext) -> bool:
    """A result that single-handedly blows the target budget.

    On the aggressive (post-overflow) path such a result loses its
    keep-last/protected-tail immunity: keeping it verbatim guarantees the
    retry fails again, while a marker (with recall/file fallback) lets the
    run make progress.
    """
    return ctx.calibrated(ctx.counter.count_entry(entry)) > ctx.budget.target_tokens


class OffloadToolResults:
    """Archive large tool results to workspace files, oldest first.

    The view keeps a marker with the file path and a short preview; the agent
    recovers the content with its workspace file tools (or the opt-in
    ``recall_tool_result`` tool, since the full output stays in the
    transcript). Inert when the run has no workspace.
    """

    name = "offload"

    def __init__(
        self,
        *,
        min_chars: int = 4_000,
        keep_last: int = 2,
        preview_chars: int = 400,
        dir: str = ".context",
        exclude_tools: Sequence[str] = (),
    ) -> None:
        """Configure offloading.

        Args:
            min_chars: Only results at least this long are archived.
            keep_last: The N most recent tool results are never archived.
            preview_chars: Length of the inline preview kept in the marker.
            dir: Workspace-relative directory for archive files.
            exclude_tools: Tool names whose results are never archived.
        """
        if min_chars < 1:
            raise ValueError("min_chars must be >= 1")
        if keep_last < 0:
            raise ValueError("keep_last must be >= 0")
        self.min_chars = min_chars
        self.keep_last = keep_last
        self.preview_chars = preview_chars
        self.dir = dir.rstrip("/")
        self.exclude_tools = frozenset(exclude_tools)

    async def plan(self, body: list[TranscriptEntry], ctx: StageContext) -> bool:
        workspace = ctx.request.workspace
        if workspace is None:
            return False
        policy = getattr(workspace, "policy", None)
        if policy is not None and getattr(policy, "allow_write", True) is False:
            return False  # read-only workspace: don't retry doomed writes
        names = _tool_names(body)
        result_idxs = _result_indices(body)
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
            if (
                len(entry.output) < self.min_chars
                or ctx.state.decided(entry.call_id)
                or names.get(entry.call_id) in self.exclude_tools
            ):
                continue
            path = f"{self.dir}/tool-{_safe_name(entry.call_id)}.txt"
            try:
                await workspace.write_text(path, entry.output)
            except Exception as exc:
                logger.warning(
                    "context.offload: write %s failed (%s: %s); skipping",
                    path,
                    type(exc).__name__,
                    exc,
                )
                continue
            record = OffloadRecord(
                path=path,
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
    the most recent ``keep_last`` results survive, excluded tools are never
    touched, small results aren't worth a marker, and clearing proceeds
    oldest-first until the view is under the target watermark.
    """

    name = "clear"

    def __init__(
        self,
        *,
        keep_last: int = 3,
        min_chars: int = 200,
        exclude_tools: Sequence[str] = (),
        clear_at_least_tokens: int | None = None,
    ) -> None:
        """Configure clearing.

        Args:
            keep_last: The N most recent tool results are never cleared
                (1 on the aggressive path).
            min_chars: Results at or below this length stay inline
                (0 on the aggressive path).
            exclude_tools: Tool names whose results are never cleared.
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
        self.exclude_tools = frozenset(exclude_tools)
        self.clear_at_least_tokens = clear_at_least_tokens

    async def plan(self, body: list[TranscriptEntry], ctx: StageContext) -> bool:
        keep_last = 1 if ctx.aggressive else self.keep_last
        min_chars = 0 if ctx.aggressive else self.min_chars
        names = _tool_names(body)
        result_idxs = _result_indices(body)
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
            if (
                len(entry.output) <= min_chars
                or ctx.state.decided(entry.call_id)
                or names.get(entry.call_id) in self.exclude_tools
            ):
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
    the prior summary text. Between bursts the existing summary is replayed
    verbatim by the renderer at zero cost.
    """

    name = "summary"

    def __init__(
        self,
        *,
        summarizer: Summarizer | None = None,
        min_savings_ratio: float = 0.10,
        max_failures: int = 3,
    ) -> None:
        """Configure summarization.

        Args:
            summarizer: Summary backend. Defaults to :class:`LLMSummarizer`
                using the run's own provider.
            min_savings_ratio: Skip the (expensive) summary call when the
                projected saving is below this fraction of the current view —
                anti-thrash. Ignored on the aggressive path.
            max_failures: Consecutive summarizer failures (per run) before
                the circuit breaker stops trying.
        """
        if not 0 <= min_savings_ratio < 1:
            raise ValueError("min_savings_ratio must be in [0, 1)")
        if max_failures < 1:
            raise ValueError("max_failures must be >= 1")
        self.summarizer: Summarizer = summarizer or LLMSummarizer()
        self.min_savings_ratio = min_savings_ratio
        self.max_failures = max_failures

    async def plan(self, body: list[TranscriptEntry], ctx: StageContext) -> bool:
        state = ctx.state
        if state.summary_failures >= self.max_failures:
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

        try:
            text = await self.summarizer.summarize(
                span,
                req=ctx.request,
                prior_summary=prior.text if prior is not None else None,
            )
        except Exception as exc:
            state.summary_failures += 1
            logger.warning(
                "context.summary: summarizer failed (%s: %s); failure %d/%d",
                type(exc).__name__,
                exc,
                state.summary_failures,
                self.max_failures,
            )
            if ctx.aggressive:
                raise
            return False

        state.summary_failures = 0
        state.summary = SummaryState(
            text=text,
            covered=new_covered,
            fingerprint=fingerprint(body[:new_covered]),
        )
        return True


def _safe_name(call_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", call_id) or "unknown"


__all__ = [
    "ClearToolResults",
    "OffloadToolResults",
    "Stage",
    "StageContext",
    "SummarizeHistory",
]
