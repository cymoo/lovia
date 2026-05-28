"""Tests for the OpenAI Chat Completions provider adapter."""

from __future__ import annotations

import asyncio

from lovia import Agent, Runner, events
from lovia.providers.openai_chat import _is_context_overflow

from .scripted_provider import ScriptedProvider, text


def test_reasoning_delta_event_emitted_by_runner() -> None:
    provider = ScriptedProvider([text("done.", reasoning="thinking...")])
    agent = Agent(name="a", model=provider)

    async def go() -> tuple[str, str]:
        handle = Runner.stream(agent, "hi")
        text_d: list[str] = []
        reasoning_d: list[str] = []
        async for ev in handle:
            if isinstance(ev, events.TextDelta):
                text_d.append(ev.delta)
            elif isinstance(ev, events.ReasoningDelta):
                reasoning_d.append(ev.delta)
        return "".join(text_d), "".join(reasoning_d)

    text_out, reasoning_out = asyncio.run(go())
    assert text_out == "done."
    assert reasoning_out == "thinking..."


# ---------------------------------------------------------------------------
# _is_context_overflow — must catch the various ways OpenAI-compatible
# endpoints surface "prompt too long" so the runner's reactive ContextPolicy
# fallback can kick in.
# ---------------------------------------------------------------------------


def test_is_context_overflow_classic_openai_code() -> None:
    body = '{"error":{"code":"context_length_exceeded","message":"..."}}'
    assert _is_context_overflow(400, body)


def test_is_context_overflow_modal_style_input_tokens_400() -> None:
    """Regression: some OpenAI-compatible gateways (modal, vLLM behind a
    proxy) phrase the 400 differently — no ``context_length_exceeded`` code,
    just human-readable text mentioning context length and ``input_tokens``.
    Before the broader matcher this slipped through as a generic
    ``ProviderError`` and the reactive ContextPolicy path never ran.
    """
    body = (
        '{"error":{"message":"You passed 131073 input tokens and requested '
        "0 output tokens. However, the model's context length is only "
        "131072 tokens, resulting in a maximum input length of 131072 "
        "tokens. Please reduce the length of the input prompt. "
        '(parameter=input_tokens, value=131073)","type":"BadRequestError",'
        '"param":"input_tokens","code":400}}'
    )
    assert _is_context_overflow(400, body)


def test_is_context_overflow_anthropic_style_413() -> None:
    assert _is_context_overflow(413, "request too large for the model")


def test_is_context_overflow_ignores_other_errors() -> None:
    assert not _is_context_overflow(400, "invalid api key")
    assert not _is_context_overflow(500, "context_length_exceeded")  # wrong status


def test_is_context_overflow_string_too_long_only_with_context() -> None:
    """``string too long`` shows up in unrelated validation errors; only
    treat it as overflow when the body also mentions context."""
    assert _is_context_overflow(400, "string too long; max context exceeded")
    assert not _is_context_overflow(400, "string too long: tool argument")
