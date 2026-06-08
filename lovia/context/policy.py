"""Context-window management for long-running agent conversations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..providers.base import (
    ModelSettings,
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
    MiddleSnipStage,
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
    """Per-turn information available to a context policy."""

    provider: Any
    model: str | None
    last_input_tokens: int | None = None
    session_id: str | None = None
    run_id: str | None = None


@dataclass
class ContextPolicyResult:
    """Structured result returned by :class:`ContextPolicy` implementations."""

    entries: list[TranscriptEntry]
    changed: bool = False
    reason: str | None = None
    summary: str | None = None
    archive_ref: ArchiveRef | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ContextPolicy(Protocol):
    """Strategy that rewrites the transcript before provider calls."""

    async def apply(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> ContextPolicyResult: ...

    async def apply_reactive(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> ContextPolicyResult: ...


@runtime_checkable
class Summarizer(Protocol):
    """Pluggable summarization backend used by :class:`CompactingContextPolicy`."""

    async def summarize(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> str: ...


class NoopContextPolicy:
    """A context policy that never modifies the transcript."""

    name = "noop"

    async def apply(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> ContextPolicyResult:
        del ctx
        return ContextPolicyResult(entries=entries)

    async def apply_reactive(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> ContextPolicyResult:
        del ctx
        return ContextPolicyResult(entries=entries)


class ProviderSummarizer:
    """Summarize a transcript by asking an LLM provider."""

    def __init__(
        self,
        provider: Any | None = None,
        *,
        prompt: str = DEFAULT_SUMMARY_PROMPT,
        settings: ModelSettings | None = None,
    ) -> None:
        self.provider = provider
        self.prompt = prompt
        self.settings = settings or ModelSettings(temperature=0)

    async def summarize(
        self, entries: list[TranscriptEntry], *, ctx: PolicyContext
    ) -> str:
        provider = self.provider or ctx.provider
        if provider is None:
            raise ValueError("ProviderSummarizer requires a provider")
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
            raise ValueError("Summarizer returned empty text")
        return summary


class CompactingContextPolicy:
    """Cheap-first context compaction with LLM summary as the last resort."""

    name = "compacting"

    def __init__(
        self,
        *,
        context_window_tokens: int | None = None,
        trigger_ratio: float = 0.8,
        max_entries: int | None = 80,
        keep_initial_entries: int = 3,
        keep_recent_entries: int = 40,
        reactive_keep_recent_entries: int = 8,
        keep_recent_tool_results: int | None = 3,
        tool_result_budget_chars: int | None = 200_000,
        large_tool_result_chars: int = 20_000,
        tool_result_preview_chars: int = 2_000,
        stages: list[ContextStage] | None = None,
        summarizer: Summarizer | None = None,
        summary_prompt: str = DEFAULT_SUMMARY_PROMPT,
        archive: CompactionArchive | None = None,
        max_summary_failures: int = 3,
    ) -> None:
        if not 0 < trigger_ratio < 1:
            raise ValueError("trigger_ratio must be between 0 and 1 (exclusive)")
        if keep_recent_entries < 1:
            raise ValueError("keep_recent_entries must be >= 1")
        if reactive_keep_recent_entries < 1:
            raise ValueError("reactive_keep_recent_entries must be >= 1")
        if keep_initial_entries < 0:
            raise ValueError("keep_initial_entries must be >= 0")
        if keep_recent_tool_results is not None and keep_recent_tool_results < 0:
            raise ValueError("keep_recent_tool_results must be >= 0")

        self.context_window_tokens = context_window_tokens
        self.trigger_ratio = trigger_ratio
        self.keep_recent_entries = keep_recent_entries
        self.reactive_keep_recent_entries = reactive_keep_recent_entries
        self.archive = archive
        self.summarizer: Summarizer = summarizer or ProviderSummarizer(
            prompt=summary_prompt
        )
        self._summary_failures = _SummaryFailures(max_failures=max_summary_failures)
        self.stages = stages or [
            ToolResultBudgetStage(
                max_chars=tool_result_budget_chars,
                large_result_chars=large_tool_result_chars,
                preview_chars=tool_result_preview_chars,
                archive=archive,
            ),
            MiddleSnipStage(
                max_entries=max_entries,
                keep_initial_entries=keep_initial_entries,
                keep_recent_entries=keep_recent_entries,
            ),
            ToolResultRetentionStage(keep_recent=keep_recent_tool_results),
        ]

    async def apply(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: PolicyContext,
    ) -> ContextPolicyResult:
        original_entries = entries
        current = entries
        changed = False
        stage_results: list[dict[str, Any]] = []

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
        result = await self._summarize(
            entries,
            ctx=ctx,
            reactive=True,
            stage_results=[],
            archive_entries=entries,
        )
        return result or ContextPolicyResult(entries=entries)

    def _threshold(self, ctx: PolicyContext) -> int | None:
        cap = self.context_window_tokens
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
        threshold = self._threshold(ctx)
        if threshold is None:
            return False
        return self._current_prompt_tokens(entries, ctx) >= threshold

    def _current_prompt_tokens(
        self,
        entries: list[TranscriptEntry],
        ctx: PolicyContext,
    ) -> int:
        estimate = _estimate_tokens(ctx.provider, entries)
        return max(estimate, ctx.last_input_tokens or 0)

    async def _summarize(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: PolicyContext,
        reactive: bool,
        stage_results: list[dict[str, Any]],
        archive_entries: list[TranscriptEntry],
    ) -> ContextPolicyResult | None:
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
        tail = (
            self.reactive_keep_recent_entries
            if reactive
            else self.keep_recent_entries
        )
        compacted = [
            make_summary_entry(summary, reactive=reactive),
            *safe_window(entries, tail=tail),
        ]
        metadata: dict[str, Any] = {
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


def _parts_to_text(parts: Any) -> str:
    try:
        from ..content import text_of

        return text_of(parts)
    except Exception:  # pragma: no cover - defensive
        return str(parts)
