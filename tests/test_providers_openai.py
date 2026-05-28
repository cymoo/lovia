"""Tests for the OpenAI Chat Completions provider adapter."""

from __future__ import annotations

import asyncio

from lovia import Agent, Runner, events

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
