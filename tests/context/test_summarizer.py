"""Tests for the LLM summarization backend (``lovia.context.summarizer``).

Exercises the pure ``transcript_to_text`` renderer, the required-section
validation, and the one-shot corrective retry that ``LLMSummarizer`` performs
when the model drops a heading.
"""

from __future__ import annotations

import pytest

from lovia.context.policy import CompactionRequest
from lovia.context.prompts import REQUIRED_SECTIONS
from lovia.context.summarizer import LLMSummarizer, transcript_to_text
from lovia.parts import ImagePart, TextPart
from lovia.transcript import (
    AssistantTextEntry,
    InputEntry,
    ReasoningEntry,
    ToolCallEntry,
    ToolResultEntry,
)

from ..scripted_provider import ScriptedProvider, text


# --------------------------------------------------------- transcript_to_text


def test_transcript_to_text_renders_each_entry_kind() -> None:
    entries = [
        InputEntry(role="user", content="hi"),
        AssistantTextEntry(content="hello"),
        ToolCallEntry(call_id="c1", name="search", arguments='{"q": "x"}'),
        ToolResultEntry(call_id="c1", output="found it"),
    ]
    assert transcript_to_text(entries) == (
        "[user] hi\n"
        "[assistant] hello\n"
        '[tool_call:search] {"q": "x"}\n'
        "[tool_result] found it"
    )


def test_transcript_to_text_skips_reasoning() -> None:
    entries = [
        ReasoningEntry(content="secret thoughts"),
        AssistantTextEntry(content="answer"),
    ]
    assert transcript_to_text(entries) == "[assistant] answer"


def test_transcript_to_text_flattens_multimodal_input() -> None:
    entries = [
        InputEntry(role="user", content=[TextPart(text="look "), ImagePart(url="u")]),
    ]
    assert transcript_to_text(entries) == "[user] look [image]"


def test_transcript_to_text_empty() -> None:
    assert transcript_to_text([]) == ""


# --------------------------------------------------------- section validation


def _full_summary() -> str:
    return "\n\n".join(f"{h}\nbody for {h}" for h in REQUIRED_SECTIONS)


def test_missing_sections_detects_case_insensitively() -> None:
    s = LLMSummarizer()
    # All sections present but lower-cased -> still complete.
    lowered = _full_summary().lower()
    assert s._missing_sections(lowered) == []


def test_missing_sections_reports_dropped_heading() -> None:
    s = LLMSummarizer()
    summary = _full_summary().replace("## Artifacts\nbody for ## Artifacts", "")
    assert s._missing_sections(summary) == ["## Artifacts"]


def test_missing_sections_disabled_with_none() -> None:
    s = LLMSummarizer(required_sections=None)
    assert s._missing_sections("anything at all") == []


# -------------------------------------------------------------- summarize ---


def _req(provider: ScriptedProvider) -> CompactionRequest:
    return CompactionRequest(entries=[], provider=provider)


async def test_summarize_requires_a_provider() -> None:
    s = LLMSummarizer()
    with pytest.raises(ValueError, match="requires a provider"):
        await s.summarize([InputEntry(role="user", content="x")], req=CompactionRequest(entries=[]))


async def test_summarize_accepts_complete_summary_without_retry() -> None:
    provider = ScriptedProvider([text(_full_summary())])
    s = LLMSummarizer(provider)
    out = await s.summarize([InputEntry(role="user", content="hi")], req=_req(provider))
    assert s._missing_sections(out) == []
    assert len(provider.calls) == 1  # no corrective retry


async def test_summarize_retries_once_when_a_section_is_missing() -> None:
    incomplete = _full_summary().replace("## Next steps\nbody for ## Next steps", "")
    provider = ScriptedProvider([text(incomplete), text(_full_summary())])
    s = LLMSummarizer(provider)
    out = await s.summarize([InputEntry(role="user", content="hi")], req=_req(provider))

    assert len(provider.calls) == 2  # one corrective retry happened
    # The retry prompt names the missing heading.
    retry_prompt = provider.calls[1][-1].content
    assert "## Next steps" in retry_prompt
    assert s._missing_sections(out) == []


async def test_summarize_ships_incomplete_summary_after_failed_retry() -> None:
    incomplete = _full_summary().replace("## Artifacts\nbody for ## Artifacts", "")
    # Both attempts come back missing the same section.
    provider = ScriptedProvider([text(incomplete), text(incomplete)])
    s = LLMSummarizer(provider)
    out = await s.summarize([InputEntry(role="user", content="hi")], req=_req(provider))

    assert len(provider.calls) == 2
    # Formatting must never block compaction; ship the best we got.
    assert s._missing_sections(out) == ["## Artifacts"]


async def test_summarize_raises_on_empty_provider_output() -> None:
    provider = ScriptedProvider([text("   ")])  # whitespace -> stripped to empty
    s = LLMSummarizer(provider)
    with pytest.raises(ValueError, match="empty text"):
        await s.summarize([InputEntry(role="user", content="hi")], req=_req(provider))


async def test_summarize_raises_on_truncated_output() -> None:
    # finish_reason "length" means the tail sections were silently cut off;
    # folding that forward would compound the loss, so it must fail loudly.
    from lovia.messages import AssistantTurn, Usage

    truncated = AssistantTurn(
        content=_full_summary(),
        usage=Usage(input_tokens=1, output_tokens=1),
        finish_reason="length",
    )
    provider = ScriptedProvider([truncated])
    s = LLMSummarizer(provider)
    with pytest.raises(ValueError, match="truncated"):
        await s.summarize([InputEntry(role="user", content="hi")], req=_req(provider))


async def test_summarize_first_vs_fold_template() -> None:
    # First summary uses the <transcript> framing.
    p1 = ScriptedProvider([text(_full_summary())])
    s = LLMSummarizer(p1)
    await s.summarize([InputEntry(role="user", content="hi")], req=_req(p1))
    assert "<transcript>" in p1.calls[0][-1].content

    # With a prior summary it folds via the <new_events> framing.
    p2 = ScriptedProvider([text(_full_summary())])
    s2 = LLMSummarizer(p2)
    await s2.summarize(
        [InputEntry(role="user", content="hi")],
        req=_req(p2),
        prior_summary="earlier summary",
    )
    folded = p2.calls[0][-1].content
    assert "<new_events>" in folded
    assert "earlier summary" in folded
