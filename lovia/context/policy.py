"""Context-window management for long-running agent conversations.

Compaction is a **pure per-call view transform**: a :class:`ContextPolicy`
turns the full transcript into the (smaller) list of entries sent to the
provider for *one* model call. It never mutates the transcript and never writes
to the :class:`~lovia.Session` — the real conversation remains the single source
of truth, so a bad summary can only affect one call, never stored history.

The default :class:`CompactingContextPolicy` does the cheapest useful thing
first and only summarises as a last resort:

1. **Stale tool results** beyond the most-recent few are replaced with a tiny
   marker (the full output stays in the transcript; the agent can pull it back
   with the optional ``recall_tool_result`` tool).
2. **An LLM summary** of the older prefix replaces it once the prompt nears the
   model's context window (or after the provider reports an overflow). The
   running summary is folded incrementally using per-run ``scratch`` state, so a
   long agentic loop summarises only the *new* span each turn rather than the
   whole prefix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

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


logger = logging.getLogger(__name__)


# Internal tuning. Demoted from constructor parameters to keep the public
# surface small; replace the whole policy if you need different behaviour.
_KEEP_TOOL_RESULTS = 3  # most-recent tool results always kept fully intact
_RETENTION_MIN_CHARS = 200  # smaller tool results are never placeholdered
_REACTIVE_KEEP_RECENT = 8  # recent entries kept after a provider overflow
_SUMMARY_FAILURE_LIMIT = 3  # consecutive summary failures before the breaker trips


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
class CompactionRequest:
    """Everything a context policy needs to produce a per-call view.

    Attributes:
        entries: The full, real transcript. **Read-only** — a policy returns a
            new list for the model call and never mutates ``entries``.
        provider: Provider selected for the next model call, if known.
        model: Model name passed to the provider.
        last_prompt_tokens: Last observed provider input-token count. This can
            lag the current transcript, so policies combine it with an estimate
            of ``entries`` rather than trusting it alone.
        session_id: Session being compacted (informational).
        run_id: Run being compacted (informational).
        overflow: ``True`` when the provider already raised
            :class:`~lovia.ContextOverflowError`; the policy should compact more
            aggressively.
        scratch: Per-run mutable state owned by the runner. A policy may cache
            derived state here (e.g. a running summary) without leaking it across
            runs — the runner creates a fresh dict for each run.
    """

    entries: list[TranscriptEntry]
    provider: Provider | None = None
    model: str | None = None
    last_prompt_tokens: int | None = None
    session_id: str | None = None
    run_id: str | None = None
    overflow: bool = False
    scratch: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextResult:
    """The per-call view a context policy produced.

    Attributes:
        entries: Transcript entries to send to the provider for this call.
        changed: Whether ``entries`` differs from the input transcript.
        reason: Stable machine-readable reason for the rewrite.
        summary: Summary text produced during compaction, when applicable.
        metadata: Extra diagnostic details emitted with compaction events.
    """

    entries: list[TranscriptEntry]
    changed: bool = False
    reason: str | None = None
    summary: str | None = None
    metadata: JsonObject = field(default_factory=dict)


@runtime_checkable
class ContextPolicy(Protocol):
    """Strategy that produces the per-call view of the transcript."""

    async def compact(self, req: CompactionRequest) -> ContextResult:
        """Return the view to send to the provider for the next model call.

        Must not mutate ``req.entries`` or persist anything — the result is used
        only for one provider call.
        """
        ...


@runtime_checkable
class ContextSummarizer(Protocol):
    """Summarization backend used by :class:`CompactingContextPolicy`."""

    async def summarize(
        self,
        entries: list[TranscriptEntry],
        *,
        req: CompactionRequest,
        prior_summary: str | None = None,
    ) -> str:
        """Return a compact natural-language summary of ``entries``.

        When ``prior_summary`` is given, ``entries`` are only the *new* events
        since that summary; the implementation should fold them in rather than
        re-summarize from scratch.
        """
        ...


class NoopContextPolicy:
    """A context policy that never modifies the transcript."""

    name = "noop"

    async def compact(self, req: CompactionRequest) -> ContextResult:
        """Return ``req.entries`` unchanged."""
        return ContextResult(entries=req.entries)


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
            provider: Provider used for summaries. When omitted, the active run
                provider from :class:`CompactionRequest` is used.
            prompt: System prompt that defines what the summary must preserve.
            settings: Provider settings for the summary call. Defaults to
                deterministic generation with ``temperature=0``.
        """
        self.provider = provider
        self.prompt = prompt
        self.settings = settings or ModelSettings(temperature=0)

    async def summarize(
        self,
        entries: list[TranscriptEntry],
        *,
        req: CompactionRequest,
        prior_summary: str | None = None,
    ) -> str:
        """Convert ``entries`` to plain text and stream a provider summary."""
        provider = self.provider or req.provider
        if provider is None:
            raise ValueError("LLMSummarizer requires a provider")
        transcript_text = _transcript_to_text(entries)
        if prior_summary:
            user = (
                "Here is the running summary of the conversation so far:\n\n"
                f"<summary>\n{prior_summary}\n</summary>\n\n"
                "Update it so it also covers these newer events, keeping the five "
                "headings and every still-relevant earlier fact:\n\n"
                f"<new_events>\n{transcript_text}\n</new_events>"
            )
        else:
            user = (
                "Summarize the following agent transcript per the rules above. "
                "Begin your response with the five headings.\n\n"
                f"<transcript>\n{transcript_text}\n</transcript>"
            )
        input_entries: list[TranscriptEntry] = [
            InputEntry(role="system", content=self.prompt),
            InputEntry(role="user", content=user),
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
    """Stale-result trimming first, an incremental LLM summary as the last resort."""

    name = "compacting"

    def __init__(
        self,
        *,
        window_tokens: int | None = None,
        trigger_ratio: float = 0.8,
        keep_recent: int = 40,
        summarizer: ContextSummarizer | None = None,
        summary_prompt: str = DEFAULT_SUMMARY_PROMPT,
    ) -> None:
        """Configure context compaction.

        Args:
            window_tokens: Model context window. When omitted, the policy asks
                the provider; if the provider does not know, proactive summary
                compaction is skipped and only the reactive overflow path runs.
            trigger_ratio: Fraction of ``window_tokens`` that triggers an LLM
                summary during proactive compaction.
            keep_recent: Number of recent entries kept inline (verbatim) when a
                summary replaces the older prefix.
            summarizer: Summary backend. Defaults to :class:`LLMSummarizer`.
            summary_prompt: Prompt used by the default summarizer.
        """
        if not 0 < trigger_ratio < 1:
            raise ValueError("trigger_ratio must be between 0 and 1 (exclusive)")
        if keep_recent < 1:
            raise ValueError("keep_recent must be >= 1")

        self.window_tokens = window_tokens
        self.trigger_ratio = trigger_ratio
        self.keep_recent = keep_recent
        self.summarizer: ContextSummarizer = summarizer or LLMSummarizer(
            prompt=summary_prompt
        )
        self._summary_failures = _SummaryFailures(max_failures=_SUMMARY_FAILURE_LIMIT)

    async def compact(self, req: CompactionRequest) -> ContextResult:
        """Trim stale tool results, then summarize only if still near capacity."""
        entries = req.entries
        view, changed, meta = self._trim_stale_tool_results(entries)

        if req.overflow or self._over_threshold(view, req):
            summarized = await self._summarize(
                req, structural_meta=meta if changed else None
            )
            if summarized is not None:
                return summarized

        if changed:
            return ContextResult(
                entries=view,
                changed=True,
                reason="context_structural",
                metadata=meta,
            )
        return ContextResult(entries=entries)

    # -- structural move -------------------------------------------------- #

    def _trim_stale_tool_results(
        self, entries: list[TranscriptEntry]
    ) -> tuple[list[TranscriptEntry], bool, JsonObject]:
        """Replace tool results older than the most-recent few with a marker.

        Returns ``(entries, False, {})`` unchanged when there is nothing to do,
        otherwise a new list. The full output stays in the real transcript; only
        the per-call view loses it.
        """
        tool_idxs = [i for i, e in enumerate(entries) if isinstance(e, ToolResultEntry)]
        keep_from = len(tool_idxs) - _KEEP_TOOL_RESULTS
        if keep_from <= 0:
            return entries, False, {}

        new: list[TranscriptEntry] | None = None
        omitted = 0
        for i in tool_idxs[:keep_from]:
            entry = entries[i]
            assert isinstance(entry, ToolResultEntry)
            if len(entry.output) <= _RETENTION_MIN_CHARS:
                continue
            if new is None:
                new = list(entries)
            new[i] = ToolResultEntry(
                call_id=entry.call_id,
                output=_stale_marker(entry.call_id),
                raw=None,
                is_error=entry.is_error,
            )
            omitted += 1

        if new is None:
            return entries, False, {}
        return new, True, {"omitted_tool_results": omitted}

    # -- summary move ----------------------------------------------------- #

    async def _summarize(
        self,
        req: CompactionRequest,
        *,
        structural_meta: JsonObject | None,
    ) -> ContextResult | None:
        """Replace the older prefix with a running summary, keep the recent tail."""
        if self._summary_failures.tripped:
            logger.warning(
                "context.summary: circuit breaker tripped after %d failures",
                self._summary_failures.count,
            )
            return None

        entries = req.entries
        if req.overflow:
            # The provider already rejected this transcript: compact harder and
            # always leave at least one older entry to summarize (when possible)
            # so reactive recovery makes progress even on a short transcript.
            tail = min(_REACTIVE_KEEP_RECENT, max(0, len(entries) - 1))
        else:
            tail = self.keep_recent
        recent = safe_window(entries, tail=tail)
        older = entries[: len(entries) - len(recent)]
        if not older:
            # Everything is inside the protected tail; nothing left to summarize.
            return None

        try:
            summary = await self._running_summary(older, req)
        except Exception as exc:
            self._summary_failures.record_failure()
            logger.warning(
                "context.summary: summarizer failed (%s: %s); failure %d/%d",
                type(exc).__name__,
                exc,
                self._summary_failures.count,
                self._summary_failures.max_failures,
            )
            if req.overflow:
                raise
            return None

        self._summary_failures.record_success()
        recent_view, _, _ = self._trim_stale_tool_results(recent)
        view = [make_summary_entry(summary, reactive=req.overflow), *recent_view]
        metadata: JsonObject = {
            "entries_before": len(entries),
            "entries_after": len(view),
            "kept_recent_entries": len(recent),
        }
        if structural_meta:
            metadata["stages"] = structural_meta
        return ContextResult(
            entries=view,
            changed=True,
            reason="reactive_summary" if req.overflow else "auto_summary",
            summary=summary,
            metadata=metadata,
        )

    async def _running_summary(
        self, older: list[TranscriptEntry], req: CompactionRequest
    ) -> str:
        """Summarize ``older``, folding in only the new span when possible.

        The transcript is append-only, so ``entries[:covered]`` is a stable
        prefix: when a prior summary covered ``covered`` entries we only need to
        fold the entries added since. This keeps a long agentic loop cheap — it
        summarizes a handful of new entries per turn, not the whole prefix.
        """
        scratch = req.scratch
        covered = scratch.get("_ctx_summary_covered", 0)
        prior = scratch.get("_ctx_summary_text")
        if prior is not None and 0 < covered <= len(older):
            new_span = older[covered:]
            if not new_span:
                return prior
            summary = await self.summarizer.summarize(
                new_span, req=req, prior_summary=prior
            )
        else:
            summary = await self.summarizer.summarize(older, req=req)
        scratch["_ctx_summary_text"] = summary
        scratch["_ctx_summary_covered"] = len(older)
        return summary

    # -- thresholds ------------------------------------------------------- #

    def _threshold(self, req: CompactionRequest) -> int | None:
        cap = self.window_tokens
        if cap is None:
            cap = _provider_context_window(req.provider, req.model)
        if cap is None:
            return None
        return max(1, int(cap * self.trigger_ratio))

    def _over_threshold(
        self, entries: list[TranscriptEntry], req: CompactionRequest
    ) -> bool:
        """Return whether proactive LLM summarization should run."""
        threshold = self._threshold(req)
        if threshold is None:
            return False
        estimate = _estimate_tokens(req.provider, entries)
        return max(estimate, req.last_prompt_tokens or 0) >= threshold


def make_summary_entry(summary: str, *, reactive: bool = False) -> InputEntry:
    """Return the user-role transcript entry that carries a context summary."""
    label = "Reactive context summary" if reactive else "Context summary"
    return InputEntry(role="user", content=f"[{label}]\n\n{summary}")


def _stale_marker(call_id: str) -> str:
    """Inline placeholder for an older tool result dropped from the view."""
    return (
        "[Earlier tool result omitted to save context. "
        f'Call recall_tool_result("{call_id}") to retrieve the full output.]'
    )


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
