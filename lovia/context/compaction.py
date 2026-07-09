"""The default context policy: a cheap-first pipeline of sticky stages.

How a call flows:

1. Load the sticky :class:`~lovia.context.state.CompactionState` from the
   per-run scratch and re-render the view. Below the *compact_at* watermark
   that's all that happens — earlier decisions are replayed verbatim, the
   prompt prefix stays byte-stable, and provider prompt caches stay warm.
2. Over the watermark (or after a provider overflow), stages run in order —
   offload, clear, summarize — each recording new sticky decisions until the
   view is under the *compact_to* watermark. The gap between the two is
   hysteresis: compaction happens in rare bursts, not every turn.
3. Token thresholds use cheap per-entry estimates *calibrated* against the
   provider's real input-token counts from previous calls (a clamped EMA
   *multiplier*). This absorbs systematic estimator error well once the
   transcript is large relative to fixed per-call overhead (tool schemas,
   system framing). On a *small* transcript with *large* tool schemas the
   multiplicative model under-counts that fixed overhead, so the proactive
   threshold can fire late — leave headroom via ``compact_at`` /
   ``reserve_output_tokens``; the reactive overflow path is the backstop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

from .policy import CompactionRequest, ContextResult
from .render import pair_safe_cuts, protected_tail_start, render_entries, render_view
from .state import (
    RATIO_MAX,
    RATIO_MIN,
    CompactionState,
    SummaryState,
    fingerprint,
    unique_result_ids,
    window_key,
)
from .stages import (
    ClearToolResults,
    OffloadToolResults,
    Stage,
    StageContext,
    SummarizeHistory,
)
from .summarizer import Summarizer
from .tokens import TokenBudget, TokenCounter, _validate_watermark
from ..providers.base import context_window as _provider_context_window
from ..transcript import TranscriptEntry, split_system

if TYPE_CHECKING:
    from ..tools import Tool
    from .store import ResultStore

logger = logging.getLogger(__name__)

# Calibration EMA: weight of the newest observation. Clamp bounds live next
# to the state (:data:`~lovia.context.state.RATIO_MIN`/``RATIO_MAX``), which
# applies them again when loading persisted scratch.
_CALIBRATION_ALPHA = 0.2

# Aggressive (post-overflow) overrides.
_REACTIVE_TARGET = 0.25


def _plural(n: int, noun: str) -> str:
    """``1 tool result`` / ``2 tool results`` — naive count + noun for notices."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


class Compaction:
    """Automatic context compaction — the default context policy.

    Every parameter has a sensible default: ``Compaction()`` asks the
    provider for the model's context window and starts compacting at 75% of
    the usable space, shrinking down to 50%. The only knob most users ever
    touch is ``context_window``::

        policy = Compaction(context_window=200_000)

    The two watermarks accept either a fraction of the usable window
    (``compact_at=0.75``) or an absolute token count
    (``compact_at=150_000``).
    """

    name = "compaction"

    def __init__(
        self,
        *,
        context_window: int | None = None,
        compact_at: int | float = 0.75,
        compact_to: int | float = 0.50,
        keep_recent_tokens: int | None = None,
        reserve_output_tokens: int = 16_384,
        stages: Sequence[Stage] | None = None,
        summarizer: Summarizer | None = None,
        image_tokens: int = 1_600,
        store: "ResultStore | None" = None,
    ) -> None:
        """Configure automatic compaction.

        Args:
            context_window: The model's context window in tokens. When
                omitted, the policy asks the provider; if the provider does
                not know either, proactive compaction is skipped and only
                the reactive overflow path runs.
            compact_at: When to start compacting — a fraction of the usable
                window (``0.75`` = 75% full) or an absolute token count
                (``150_000``).
            compact_to: How far a compaction burst shrinks the prompt, in
                the same units. Must resolve below ``compact_at``; the gap
                is the anti-thrash hysteresis.
            keep_recent_tokens: Token budget for the verbatim recent tail
                that compaction never touches. Defaults to 20% of the
                usable window.
            reserve_output_tokens: Headroom reserved for the model's reply.
            stages: Stage chain. Defaults to
                ``[OffloadToolResults(), ClearToolResults(), SummarizeHistory()]``.
            summarizer: Summary backend for the default summarize stage.
                Ignored when ``stages`` is given explicitly.
            image_tokens: Flat token cost per image for the estimator.
            store: Optional durable sink where :class:`OffloadToolResults`
                archives large results, and where the provided
                ``recall_tool_result`` reads them back. ``None`` (default)
                keeps offload active — it still emits preview markers and recall
                falls back to the transcript. Pass
                :class:`~lovia.context.FileResultStore` when that fallback isn't
                enough: the transcript holds every output today, but a clearing
                policy (or a restart) can drop it, and the store survives that.
        """
        if context_window is not None and context_window < 1:
            raise ValueError("context_window must be >= 1")
        if reserve_output_tokens < 0:
            raise ValueError("reserve_output_tokens must be >= 0")
        _validate_watermark(compact_at, "compact_at")
        _validate_watermark(compact_to, "compact_to")
        if type(compact_at) is type(compact_to) and compact_to >= compact_at:
            raise ValueError("compact_to must be below compact_at")
        if keep_recent_tokens is not None and keep_recent_tokens < 1:
            raise ValueError("keep_recent_tokens must be >= 1")

        self.context_window = context_window
        self.reserve_output_tokens = reserve_output_tokens
        self.compact_at = compact_at
        self.compact_to = compact_to
        self.keep_recent_tokens = keep_recent_tokens
        self.image_tokens = image_tokens
        self.stages: list[Stage] = (
            list(stages)
            if stages is not None
            else [
                OffloadToolResults(),
                ClearToolResults(),
                SummarizeHistory(summarizer=summarizer),
            ]
        )
        self.store = store
        # Cache the most-recent provider's counter, keyed by provider *identity*
        # via a strong ref — NOT id(): a cached provider can't then be GC'd and
        # have its id() reused by a different provider (which would hand back the
        # wrong tokenizer). At most one provider is pinned at a time.
        self._counter: tuple[object | None, TokenCounter] | None = None

    async def compact(self, req: CompactionRequest) -> ContextResult:
        """Replay sticky decisions; make new ones only under token pressure."""
        state = CompactionState.load(req.scratch)
        _, body = split_system(req.entries)

        # The running summary is carried across runs (in the segment ``meta``),
        # so the body prefix it claims to cover can differ from the live one —
        # e.g. a follow-up run whose session history was trimmed or rewritten.
        # Detect that and drop the summary (clear/offload records are keyed by
        # call_id and survive such rewrites).
        summary = state.summary
        if summary is not None and (
            not 0 < summary.covered <= len(body)
            or fingerprint(body[: summary.covered]) != summary.fingerprint
        ):
            logger.info(
                "context: covered transcript prefix changed; resetting running summary"
            )
            state.summary = None
            summary = None

        # A coverage frontier inside a tool call/result pair (persisted by an
        # interrupted fold, or scratch written by an older lovia) would render
        # a view whose first post-summary entry is an orphaned tool result —
        # providers hard-reject that, and the sticky state would replay it on
        # every retry. Rewind to the nearest pair-safe cut: the re-exposed
        # entries are already in the summary text, so duplication is the only
        # cost.
        if summary is not None:
            safe = pair_safe_cuts(body)
            if not safe[summary.covered]:
                covered = summary.covered
                while covered > 0 and not safe[covered]:
                    covered -= 1
                logger.info(
                    "context: summary coverage split a tool call/result pair; "
                    "rewinding %d -> %d",
                    summary.covered,
                    covered,
                )
                state.summary = (
                    SummaryState(
                        text=summary.text,
                        covered=covered,
                        fingerprint=fingerprint(body[:covered]),
                    )
                    if covered > 0
                    else None
                )

        # GC clear/offload records that no longer point at exactly one live
        # tool result — ids trimmed out of the session history, or ids a
        # provider reused (which would make the marker replace the wrong,
        # possibly newest, result). Keeps the persisted scratch from growing
        # with records nothing can render.
        state.prune(unique_result_ids(body))

        # Calibrate the estimator against the real usage of the previous call.
        if req.last_input_tokens and state.last_view_estimate:
            observed = req.last_input_tokens / max(1, state.last_view_estimate)
            state.ratio = min(
                RATIO_MAX,
                max(
                    RATIO_MIN,
                    (1 - _CALIBRATION_ALPHA) * state.ratio
                    + _CALIBRATION_ALPHA * observed,
                ),
            )

        counter = self._counter_for(req.provider)
        view = render_view(req.entries, state)
        raw = counter.count(view)
        tokens = int(raw * state.ratio)
        tokens_before = int(counter.count(req.entries) * state.ratio)

        aggressive = req.overflow
        window = self.context_window
        if window is None:
            window = _provider_context_window(req.provider, req.model)
        # The endpoint's own rejection outranks every other source: a
        # configured or tabled window is a *claim*, the number in a 400 is the
        # limit being enforced. It only ever caps — a user who deliberately
        # budgets below the real window keeps their smaller number.
        learned = self._learn_window(req, state)
        if learned is not None:
            window = learned if window is None else min(window, learned)
        # The real window (when known) survives the aggressive override below:
        # stages that make actual model calls (summarize) size against it.
        model_window = window
        if window is None and not aggressive:
            # No budget information: never compact proactively; the
            # reactive overflow path remains as the safety net.
            return self._result(req, state, view, raw, tokens, tokens_before, [], None)
        if aggressive:
            # An overflow proves the effective limit is at most the failed
            # prompt itself — any configured/claimed window is now refuted,
            # so budget against the actual prompt size to guarantee stages
            # have room to shrink.
            window = max(tokens, 1) + self.reserve_output_tokens
        assert window is not None

        budget = TokenBudget(
            window=window,
            reserve_output=self.reserve_output_tokens,
            trigger=self.compact_at,
            target=self.compact_to,
        )
        if aggressive:
            # Tighten the target on *resolved* token counts so fraction and
            # absolute watermarks compare in the same units.
            budget = TokenBudget(
                window=window,
                reserve_output=self.reserve_output_tokens,
                trigger=self.compact_at,
                target=min(
                    budget.target_tokens,
                    max(1, int(_REACTIVE_TARGET * budget.usable)),
                ),
            )
        if not aggressive and tokens < budget.trigger_tokens:
            return self._result(
                req, state, view, raw, tokens, tokens_before, [], budget
            )

        tail_tokens = self.keep_recent_tokens or max(1, budget.usable // 5)
        if aggressive:
            tail_tokens = min(tail_tokens, max(1, budget.usable // 10))
        # Measure the tail on *rendered* entries (1:1 index-aligned with the
        # body): already-cleared/offloaded results cost marker-size in the
        # actual prompt, so counting them raw would fill the tail budget with
        # phantom tokens and leave the model less verbatim recency than
        # ``keep_recent_tokens`` promises.
        protected_from = protected_tail_start(
            render_entries(body, state), counter, state.ratio, tail_tokens
        )

        reasons: list[str] = []
        try:
            for stage in self.stages:
                ctx = StageContext(
                    request=req,
                    state=state,
                    counter=counter,
                    budget=budget,
                    current_tokens=tokens,
                    protected_from=protected_from,
                    aggressive=aggressive,
                    store=self.store,
                    model_window=model_window,
                )
                if await stage.plan(body, ctx):
                    reasons.append(stage.name)
                    view = render_view(req.entries, state)
                    raw = counter.count(view)
                    tokens = int(raw * state.ratio)
                if tokens <= budget.target_tokens:
                    break
        except BaseException:
            # Stages are expected to log-and-return-False on failure, but an
            # unexpected raise must still keep what was already decided (and
            # the failure counters).
            state.last_view_estimate = raw
            self._save(req, state)
            raise

        if reasons:
            logger.info(
                "context.compact: %s — %d → %d est. tokens (pressure %.2f)",
                "+".join(reasons),
                tokens_before,
                tokens,
                budget.pressure(tokens),
            )
        return self._result(
            req, state, view, raw, tokens, tokens_before, reasons, budget
        )

    def tools(self) -> list["Tool"]:
        """Provide ``recall_tool_result``, bound to this policy's store.

        The runner injects it whenever this policy is active, so the markers
        this policy renders always have a tool to back them.
        """
        from ..tools.recall import make_recall_tool

        return [make_recall_tool(self.store)]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _result(
        self,
        req: CompactionRequest,
        state: CompactionState,
        view: list[TranscriptEntry],
        raw: int,
        tokens: int,
        tokens_before: int,
        reasons: list[str],
        budget: TokenBudget | None,
    ) -> ContextResult:
        state.last_view_estimate = raw
        self._save(req, state)

        changed = _differs(view, req.entries)
        if reasons:
            reason = ("reactive_" if req.overflow else "") + "+".join(reasons)
        elif changed:
            reason = "sticky_replay"
        else:
            reason = None

        # Policy-authored notice bullets, rendered verbatim by the UI. Only the
        # decisions worth surfacing — the calibration ratio stays internal.
        # Counts are cumulative session state, and say so: a notice fires per
        # burst, but its numbers describe everything decided up to now.
        detail: list[str] = []
        if budget is not None:
            detail.append(f"context was {round(budget.pressure(tokens) * 100)}% full")
        if state.offloaded:
            detail.append(
                f"{_plural(len(state.offloaded), 'tool result')} offloaded in total"
            )
        if state.cleared:
            detail.append(
                f"{_plural(len(state.cleared), 'tool result')} cleared in total"
            )
        if state.summary is not None:
            detail.append(f"summary covers {_plural(state.summary.covered, 'message')}")
        return ContextResult(
            entries=view,
            changed=changed,
            compacted=bool(reasons),
            reason=reason,
            summary=(
                state.summary.text
                if state.summary is not None and SummarizeHistory.name in reasons
                else None
            ),
            tokens_before=tokens_before,
            tokens_after=tokens,
            detail=detail,
        )

    def _learn_window(
        self, req: CompactionRequest, state: CompactionState
    ) -> int | None:
        """Record and return the window this endpoint reported for the model.

        A fresh ``reported_window`` replaces any earlier one: both are direct
        statements from the endpoint, and the newest describes the deployment
        as it is now.
        """
        key = window_key(req.provider, req.model)
        reported = req.reported_window
        if reported is not None and state.learned_windows.get(key) != reported:
            logger.info(
                "context.window: learned %d tokens for %r from the provider's "
                "overflow response",
                reported,
                req.model,
            )
            state.learned_windows[key] = reported
        return state.learned_windows.get(key)

    def _save(self, req: CompactionRequest, state: CompactionState) -> None:
        """Persist ``state`` into the per-run scratch."""
        state.save(req.scratch)

    def _counter_for(self, provider: object | None) -> TokenCounter:
        cached = self._counter
        if cached is not None and cached[0] is provider:
            return cached[1]
        counter = TokenCounter(provider, image_tokens=self.image_tokens)
        self._counter = (provider, counter)
        return counter


def _differs(view: list[TranscriptEntry], entries: list[TranscriptEntry]) -> bool:
    """Identity walk: did rendering substitute anything?"""
    return len(view) != len(entries) or any(a is not b for a, b in zip(view, entries))


__all__ = ["Compaction"]
