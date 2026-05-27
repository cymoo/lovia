"""Tests for the Phase-1 content/reasoning/typed-context features."""

from __future__ import annotations

from dataclasses import dataclass

from lovia import Agent, ImageBlock, Runner, TextBlock, events, user
from lovia.messages import ChatMessage
from lovia.providers.anthropic import _to_anthropic_messages

from .scripted_provider import ScriptedProvider, text


def test_text_block_serializes_to_openai_parts() -> None:
    msg = user([TextBlock("hello"), ImageBlock(url="https://x/y.png")])
    payload = msg.as_openai()
    assert payload["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]


def test_image_block_base64_serializes_with_data_url() -> None:
    msg = user(ImageBlock(data="ZmFrZQ==", mime_type="image/png"))
    payload = msg.as_openai()
    assert payload["content"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_image_block_requires_exactly_one_source() -> None:
    import pytest

    with pytest.raises(ValueError):
        ImageBlock()
    with pytest.raises(ValueError):
        ImageBlock(url="x", data="y", mime_type="image/png")


def test_anthropic_translates_image_blocks() -> None:
    msgs = [
        ChatMessage(
            role="user",
            content=[
                TextBlock("describe"),
                ImageBlock(url="https://x/y.png"),
                ImageBlock(data="ZmFrZQ==", mime_type="image/png"),
            ],
        )
    ]
    _, out = _to_anthropic_messages(msgs)
    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://x/y.png"},
    }
    assert parts[2]["type"] == "image"
    assert parts[2]["source"]["type"] == "base64"
    assert parts[2]["source"]["media_type"] == "image/png"


def test_reasoning_delta_event_emitted() -> None:
    import asyncio

    provider = ScriptedProvider([text("done.", reasoning="thinking...")])
    agent = Agent(name="a", model=provider)

    async def go() -> tuple[list[str], list[str]]:
        handle = Runner.run_streamed(agent, "hi")
        text_d: list[str] = []
        reasoning_d: list[str] = []
        async for ev in handle:
            if isinstance(ev, events.TextDelta):
                text_d.append(ev.delta)
            elif isinstance(ev, events.ReasoningDelta):
                reasoning_d.append(ev.delta)
        return text_d, reasoning_d

    text_d, reasoning_d = asyncio.run(go())
    assert "".join(text_d) == "done."
    assert "".join(reasoning_d) == "thinking..."


def test_typed_context_inferred() -> None:
    """Static-typing smoke test: the run result is parameterized by Agent's TOutput."""

    @dataclass
    class Order:
        id: str
        qty: int

    agent: Agent[Order, str] = Agent(name="a", model=ScriptedProvider([text("ok")]))

    import asyncio

    async def go() -> str:
        # ``context`` accepts an Order; the runtime check is just that
        # the call doesn't blow up — the static check is the real value.
        res = await Runner.run(agent, "ping", context=Order(id="o1", qty=1))
        return res.output

    assert asyncio.run(go()) == "ok"
