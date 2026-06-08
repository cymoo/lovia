"""Context-window management for long-running agent conversations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .._types import JsonObject
from ..content import ContentPart
from ..providers.base import (
    ModelSettings,
    Provider,
    context_window as _provider_context_window,
    estimate_tokens as _estimate_tokens,
)
from ..transcript import (
    AssistantTextEntry,
    InputEntry,
    ReasoningEntry,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
    safe_window,
)
from .archive import ArchiveRef, CompactionArchive
from .stages import (
    ContextStage,
    MiddleTrimStage,
    ToolResultBudgetStage,
    ToolResultRetentionStage,
)


logger = logging.getLogger(__name__)


DEFAULT_SUMMARY_PROMPT = """\
You are compacting a long agent transcript so the conversation can continue \
without losing important state. Produce a faithful, third-person summary \
covering ONLY what is needed for the next turns:

1. **User goal(s)** — what the user is ultimately trying to achieve.
2. **Key findings & decisions** — facts established, conclusions reached, \
   approaches ruled out (with brief justification).
3. **Files / artifacts changed** — paths touched, what changed, and why.
4. **Outstanding work** — concrete next steps the assistant intended to take.
5. **User constraints & preferences** — explicit rules the user gave (tone, \
   style, must/must-not-do, deadlines, names, IDs, etc.).

Rules:
- Be specific. Preserve exact identifiers, file paths, and numbers verbatim.
- Do NOT invent facts not present in the transcript.
- Do NOT include pleasantries or meta-commentary about the summarization.
- Output plain prose with the five headings above; no JSON, no code fences.
"""


@dataclass
class PolicyContext:
    """Per-turn information available to a context policy.

    Attributes:
        provider: Provider selected for the next model call, if one is known.
        model: Model name passed to the provider.
        last_prompt_tokens: Last observed provider input-token count. This can
            lag behind the current transcript, so policies should combine it
            with an estimate of ``entries`` rather than trusting it alone.
        session_id: Session being compacted, used by archives.
        run_id: Run being compacted, used by archives.
    """

    provider: Provider | None
    model: str | None
    last_prompt_tokens: int | None = None
    session_id: str | None = None
    run_id: str | None = None


@dataclass
class ContextPolicyResult:
    """Structured result returned by :class:`ContextPolicy` implementations.

    Attributes:
        entries: Transcript entries to use for the next provider call.
        changed: Whether ``entries`` differs from the input transcript.
        reason: Stable machine-readable reason for the rewrite.
        summary: Summary text produced during compaction, when applicable.
        archive_ref: Reference to the archived original transcript, if saved.
        metadata: Extra diagnostic details emitted with compaction events.
    """

    entries: list[TranscriptEntry]
    changed: bool = False
    reason: str | None = None
    summary: str | None = None
    archive_ref: ArchiveRef | None = None
    metadata: JsonObject = field(default_factory=dict)


@runtime_checkable
class ContextPolicy(Protocol):
    """Strategy that rewrites the transcript before provider calls."""

    async def apply(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> ContextPolicyResult:
        """Rewrite ``entries`` proactively before a provider call."""
        ...

    async def apply_reactive(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> ContextPolicyResult:
        """Rewrite ``entries`` after a provider reports context overflow."""
        ...


@runtime_checkable
class ContextSummarizer(Protocol):
    """Summarization backend used by :class:`CompactingContextPolicy`."""

    async def summarize(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> str:
        """Return a compact natural-language summary of ``entries``."""
        ...


class NoopContextPolicy:
    """A context policy that never modifies the transcript."""

    name = "noop"

    async def apply(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> ContextPolicyResult:
        """Return ``entries`` unchanged during proactive compaction."""
        del ctx
        return ContextPolicyResult(entries=entries)

    async def apply_reactive(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> ContextPolicyResult:
        """Return ``entries`` unchanged after context overflow."""
        del ctx
        return ContextPolicyResult(entries=entries)


class LLMSummarizer:
    """Summarize a transcript by asking an LLM provider."""

    def __init__(
        self,
        provider: Provider | None = None,
        *,
        prompt: str = DEFAULT_SUMMARY_PROMPT,
        settings: ModelSettings | None = None,
    ) -> None:
        """Create an LLM-backed summarizer.

        Args:
            provider: Provider used for summaries. When omitted, the active
                run provider from :class:`PolicyContext` is used.
            prompt: System prompt that defines what the summary must preserve.
            settings: Provider settings for the summary call. Defaults to
                deterministic generation with ``temperature=0``.
        """
        self.provider = provider
        self.prompt = prompt
        self.settings = settings or ModelSettings(temperature=0)

    async def summarize(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> str:
        """Convert ``entries`` to plain text and stream a provider summary."""
        provider = self.provider or ctx.provider
        if provider is None:
            raise ValueError("LLMSummarizer requires a provider")
        transcript_text = _transcript_to_text(entries)
        input_entries: list[TranscriptEntry] = [
            InputEntry(role="system", content=self.prompt),
            InputEntry(
                role="user",
                content=(
                    "Summarize the following agent transcript per the rules above. "
                    "Begin your response with the five headings.\n\n"
                    "<transcript>\n" + transcript_text + "\n</transcript>"
                ),
            ),
        ]
        chunks: list[str] = []
        async for delta in provider.stream(input_entries, settings=self.settings):
            text = getattr(delta, "text", None)
            if isinstance(text, str) and getattr(delta, "type", "") == "text_delta":
                chunks.append(text)
        summary = "".join(chunks).strip()
        if not summary:
            raise ValueError("LLMSummarizer returned empty text")
        return summary


class CompactingContextPolicy:
    """Cheap-first context compaction with LLM summary as the last resort."""

    name = "compacting"

    def __init__(
        self,
        *,
        window_tokens: int | None = None,
        trigger_ratio: float = 0.8,
        max_entries: int | None = 80,
        keep_initial: int = 3,
        keep_recent: int = 40,
        reactive_keep_recent: int = 8,
        keep_tool_results: int | None = 3,
        max_tool_result_chars: int | None = 200_000,
        large_tool_result_chars: int = 20_000,
        tool_preview_chars: int = 2_000,
        stages: list[ContextStage] | None = None,
        summarizer: ContextSummarizer | None = None,
        summary_prompt: str = DEFAULT_SUMMARY_PROMPT,
        archive: CompactionArchive | None = None,
        summary_failure_limit: int = 3,
    ) -> None:
        """Configure context compaction.

        Args:
            window_tokens: Model context window. When omitted, the policy asks
                the provider; if the provider does not know, proactive summary
                compaction is skipped.
            trigger_ratio: Fraction of ``window_tokens`` that triggers an LLM
                summary during proactive compaction.
            max_entries: Maximum transcript entries before the middle-trim
                stage removes older middle entries. ``None`` disables this
                stage.
            keep_initial: Number of leading entries to preserve when trimming
                the middle of a long transcript.
            keep_recent: Number of recent entries to preserve for proactive
                summaries and middle trimming.
            reactive_keep_recent: Number of recent entries to preserve after a
                provider reports context overflow.
            keep_tool_results: Number of most recent tool results to leave
                intact. ``None`` disables placeholder replacement for older
                tool results.
            max_tool_result_chars: Total character budget for tool-result
                output before large results are archived or previewed. ``None``
                disables this budget.
            large_tool_result_chars: Minimum size for a single tool result to
                be eligible for archive/preview replacement.
            tool_preview_chars: Number of leading characters kept in the inline
                preview when a large tool result is replaced.
            stages: Custom cheap structural stages. When provided, these
                replace the default tool-budget, middle-trim, and retention
                stages.
            summarizer: Summary backend. Defaults to :class:`LLMSummarizer`.
            summary_prompt: Prompt used by the default summarizer.
            archive: Optional sink for transcripts and large tool results that
                leave the active model context.
            summary_failure_limit: Proactive summary failures allowed before
                the circuit breaker stops retrying summaries.
        """
        if not 0 < trigger_ratio < 1:
            raise ValueError("trigger_ratio must be between 0 and 1 (exclusive)")
        if keep_recent < 1:
            raise ValueError("keep_recent must be >= 1")
        if reactive_keep_recent < 1:
            raise ValueError("reactive_keep_recent must be >= 1")
        if keep_initial < 0:
            raise ValueError("keep_initial must be >= 0")
        if keep_tool_results is not None and keep_tool_results < 0:
            raise ValueError("keep_tool_results must be >= 0")

        self.window_tokens = window_tokens
        self.trigger_ratio = trigger_ratio
        self.keep_recent = keep_recent
        self.reactive_keep_recent = reactive_keep_recent
        self.archive = archive
        self.summarizer: ContextSummarizer = summarizer or LLMSummarizer(
            prompt=summary_prompt
        )
        self._summary_failures = _SummaryFailures(max_failures=summary_failure_limit)
        self.stages = stages or [
            ToolResultBudgetStage(
                max_chars=max_tool_result_chars,
                large_result_chars=large_tool_result_chars,
                preview_chars=tool_preview_chars,
                archive=archive,
            ),
            MiddleTrimStage(
                max_entries=max_entries,
                keep_initial=keep_initial,
                keep_recent=keep_recent,
            ),
            ToolResultRetentionStage(keep_recent=keep_tool_results),
        ]

    async def apply(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: PolicyContext,
    ) -> ContextPolicyResult:
        """Run cheap stages, then summarize only if the prompt is near capacity."""
        original_entries = entries
        current = entries
        changed = False
        stage_results: list[JsonObject] = []

        for stage in self.stages:
            result = await stage.apply(current, ctx=ctx)
            if result.changed:
                current = result.entries
                changed = True
                stage_results.append(
                    {
                        "stage": stage.name,
                        "reason": result.reason,
                        **result.metadata,
                    }
                )

        if self._should_summarize(current, ctx):
            summary_result = await self._summarize(
                current,
                ctx=ctx,
                reactive=False,
                stage_results=stage_results,
                archive_entries=original_entries,
            )
            if summary_result is not None:
                return summary_result

        if changed:
            return ContextPolicyResult(
                entries=current,
                changed=True,
                reason="context_stages",
                metadata={"stages": stage_results},
            )
        return ContextPolicyResult(entries=entries)

    async def apply_reactive(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: PolicyContext,
    ) -> ContextPolicyResult:
        """Summarize immediately after a provider context-overflow error."""
        result = await self._summarize(
            entries,
            ctx=ctx,
            reactive=True,
            stage_results=[],
            archive_entries=entries,
        )
        return result or ContextPolicyResult(entries=entries)

    def _threshold(self, ctx: PolicyContext) -> int | None:
        cap = self.window_tokens
        if cap is None:
            cap = _provider_context_window(ctx.provider, ctx.model)
        if cap is None:
            return None
        return max(1, int(cap * self.trigger_ratio))

    def _should_summarize(
        self,
        entries: list[TranscriptEntry],
        ctx: PolicyContext,
    ) -> bool:
        """Return whether proactive LLM summarization should run."""
        threshold = self._threshold(ctx)
        if threshold is None:
            return False
        return self._current_prompt_tokens(entries, ctx) >= threshold

    def _current_prompt_tokens(
        self,
        entries: list[TranscriptEntry],
        ctx: PolicyContext,
    ) -> int:
        """Return the best known current prompt size."""
        estimate = _estimate_tokens(ctx.provider, entries)
        return max(estimate, ctx.last_prompt_tokens or 0)

    async def _summarize(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: PolicyContext,
        reactive: bool,
        stage_results: list[JsonObject],
        archive_entries: list[TranscriptEntry],
    ) -> ContextPolicyResult | None:
        """Archive the original transcript and replace history with a summary."""
        reason = "reactive_summary" if reactive else "auto_summary"
        if self._summary_failures.tripped:
            logger.warning(
                "context.summary: circuit breaker tripped after %d failures",
                self._summary_failures.count,
            )
            return None

        archive_ref = await self._archive_transcript(
            archive_entries,
            ctx=ctx,
            reason=reason,
        )
        try:
            summary = await self.summarizer.summarize(entries, ctx=ctx)
        except Exception as exc:
            self._summary_failures.record_failure()
            logger.warning(
                "context.summary: summarizer failed (%s: %s); failure %d/%d",
                type(exc).__name__,
                exc,
                self._summary_failures.count,
                self._summary_failures.max_failures,
            )
            if reactive:
                raise
            return None

        self._summary_failures.record_success()
        tail = self.reactive_keep_recent if reactive else self.keep_recent
        compacted = [
            make_summary_entry(summary, reactive=reactive),
            *safe_window(entries, tail=tail),
        ]
        metadata: JsonObject = {
            "entries_before": len(entries),
            "entries_after": len(compacted),
            "kept_recent_entries": tail,
        }
        if stage_results:
            metadata["stages"] = stage_results
        return ContextPolicyResult(
            entries=compacted,
            changed=True,
            reason=reason,
            summary=summary,
            archive_ref=archive_ref,
            metadata=metadata,
        )

    async def _archive_transcript(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: PolicyContext,
        reason: str,
    ) -> ArchiveRef | None:
        """Persist ``entries`` when an archive is configured."""
        if self.archive is None:
            return None
        try:
            return await self.archive.save_transcript(entries, ctx=ctx, reason=reason)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "context.archive.transcript_failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return None


def make_summary_entry(summary: str, *, reactive: bool = False) -> InputEntry:
    """Return the user-role transcript entry that carries a context summary."""
    label = "Reactive context summary" if reactive else "Context summary"
    return InputEntry(role="user", content=f"[{label}]\n\n{summary}")


@dataclass
class _SummaryFailures:
    max_failures: int = 3
    count: int = 0

    def record_success(self) -> None:
        self.count = 0

    def record_failure(self) -> None:
        self.count += 1

    @property
    def tripped(self) -> bool:
        return self.count >= self.max_failures


def _transcript_to_text(entries: list[TranscriptEntry]) -> str:
    """Render transcript entries as plain text for the summarizer prompt."""
    out: list[str] = []
    for entry in entries:
        if isinstance(entry, InputEntry):
            content = (
                entry.content
                if isinstance(entry.content, str)
                else _parts_to_text(entry.content)
            )
            out.append(f"[{entry.role}] {content}")
        elif isinstance(entry, AssistantTextEntry):
            out.append(f"[assistant] {entry.content}")
        elif isinstance(entry, ReasoningEntry):
            continue
        elif isinstance(entry, ToolCallEntry):
            out.append(f"[tool_call:{entry.name}] {entry.arguments}")
        elif isinstance(entry, ToolResultEntry):
            out.append(f"[tool_result] {entry.output}")
    return "\n".join(out)


def _parts_to_text(parts: list[ContentPart]) -> str:
    """Best-effort text extraction for multimodal input parts."""
    try:
        from ..content import text_of

        return text_of(parts)
    except Exception:  # pragma: no cover - defensive
        return str(parts)
