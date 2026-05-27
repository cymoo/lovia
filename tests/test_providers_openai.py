"""Tests for the OpenAI Chat Completions provider adapter."""

from __future__ import annotations

import asyncio

from lovia import Agent, Runner, events
from lovia.providers.openai_chat import _parse_completion

from .scripted_provider import ScriptedProvider, text


def test_parse_completion_extracts_cached_tokens() -> None:
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 7},
        },
    }
    msg = _parse_completion(data)
    assert msg.content == "hi"
    assert msg.usage.input_tokens == 20
    assert msg.usage.output_tokens == 4
    assert msg.usage.cache_read_tokens == 7


def test_parse_completion_handles_missing_usage_details() -> None:
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    msg = _parse_completion(data)
    assert msg.usage.cache_read_tokens == 0


def test_reasoning_delta_event_emitted_by_runner() -> None:
    provider = ScriptedProvider([text("done.", reasoning="thinking...")])
    agent = Agent(name="a", model=provider)

    async def go() -> tuple[str, str]:
        handle = Runner.run_streamed(agent, "hi")
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
