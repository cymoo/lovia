"""Summarization backends for the summarize stage.

:class:`LLMSummarizer` asks a provider for a structured summary with the
sections in :data:`~lovia.context.prompts.REQUIRED_SECTIONS` and supports
*incremental folding*: given a prior summary, only the new events are sent
and the model updates the summary in place — a long agentic loop summarizes
a handful of new entries per burst, not the whole prefix.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .policy import CompactionRequest
from .prompts import (
    REQUIRED_SECTIONS,
    SUMMARY_FIRST_TEMPLATE,
    SUMMARY_FOLD_TEMPLATE,
    SUMMARY_SYSTEM_PROMPT,
)
from ..parts import ContentPart, text_of
from ..providers.base import ModelSettings, Provider
from ..transcript import (
    AssistantTextEntry,
    InputEntry,
    ReasoningEntry,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class Summarizer(Protocol):
    """Summarization backend used by the summarize stage."""

    async def summarize(
        self,
        entries: list[TranscriptEntry],
        *,
        req: CompactionRequest,
        prior_summary: str | None = None,
    ) -> str:
        """Return a compact natural-language summary of ``entries``.

        When ``prior_summary`` is given, ``entries`` are only the *new*
        events since that summary; the implementation should fold them in
        rather than re-summarize from scratch.
        """
        ...


class LLMSummarizer:
    """Summarize a transcript span by asking an LLM provider."""

    def __init__(
        self,
        provider: Provider | None = None,
        *,
        prompt: str = SUMMARY_SYSTEM_PROMPT,
        settings: ModelSettings | None = None,
        required_sections: tuple[str, ...] | None = REQUIRED_SECTIONS,
    ) -> None:
        """Create an LLM-backed summarizer.

        Args:
            provider: Provider used for summaries. When omitted, the active
                run provider from :class:`CompactionRequest` is used.
            prompt: System prompt that defines what the summary must preserve.
            settings: Provider settings for the summary call. Defaults to
                deterministic generation with ``temperature=0``.
            required_sections: Headings the summary must contain. A missing
                heading triggers one corrective retry, then is accepted with
                a warning. Pass ``None`` to disable validation (e.g. with a
                custom free-form ``prompt``).
        """
        self.provider = provider
        self.prompt = prompt
        self.settings = settings or ModelSettings(temperature=0)
        self.required_sections = required_sections

    async def summarize(
        self,
        entries: list[TranscriptEntry],
        *,
        req: CompactionRequest,
        prior_summary: str | None = None,
    ) -> str:
        """Render ``entries`` as text and stream a structured summary."""
        provider = self.provider or req.provider
        if provider is None:
            raise ValueError("LLMSummarizer requires a provider")
        events_text = transcript_to_text(entries)
        if prior_summary:
            user = SUMMARY_FOLD_TEMPLATE.format(prior=prior_summary, events=events_text)
        else:
            user = SUMMARY_FIRST_TEMPLATE.format(events=events_text)
        conversation: list[TranscriptEntry] = [
            InputEntry(role="system", content=self.prompt),
            InputEntry(role="user", content=user),
        ]
        summary = await self._generate(provider, conversation)

        missing = self._missing_sections(summary)
        if missing:
            retry = conversation + [
                AssistantTextEntry(content=summary),
                InputEntry(
                    role="user",
                    content=(
                        "Your summary is missing the section(s): "
                        + ", ".join(missing)
                        + ". Re-emit the complete summary with ALL required "
                        "headings, keeping every fact you already wrote."
                    ),
                ),
            ]
            summary = await self._generate(provider, retry)
            still_missing = self._missing_sections(summary)
            if still_missing:
                # Formatting must never block compaction; ship it as-is.
                logger.warning(
                    "context.summary: sections still missing after retry: %s",
                    ", ".join(still_missing),
                )
        return summary

    async def _generate(
        self, provider: Provider, conversation: list[TranscriptEntry]
    ) -> str:
        chunks: list[str] = []
        async for delta in provider.stream(conversation, settings=self.settings):
            text = getattr(delta, "text", None)
            if isinstance(text, str) and getattr(delta, "type", "") == "text_delta":
                chunks.append(text)
        summary = "".join(chunks).strip()
        if not summary:
            raise ValueError("LLMSummarizer returned empty text")
        return summary

    def _missing_sections(self, summary: str) -> list[str]:
        if not self.required_sections:
            return []
        lowered = summary.lower()
        return [s for s in self.required_sections if s.lower() not in lowered]


def transcript_to_text(entries: list[TranscriptEntry]) -> str:
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
        return text_of(parts)
    except Exception:  # pragma: no cover - defensive
        return str(parts)


__all__ = ["LLMSummarizer", "Summarizer", "transcript_to_text"]
