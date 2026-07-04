"""``HumanChannel.questions()`` — the push-based consumer loop."""

from __future__ import annotations

import asyncio

import pytest

from lovia import Agent, Runner
from lovia.exceptions import ToolError
from lovia.testing import ScriptedProvider, call, text
from lovia.tools.human import HumanChannel, HumanQuestion, ask_human


async def test_questions_yields_and_close_ends_iteration() -> None:
    channel = HumanChannel()
    seen: list[HumanQuestion] = []

    async def operator() -> None:
        async for q in channel.questions():
            seen.append(q)
            channel.answer(q.id, f"answer to {q.question}")

    op = asyncio.create_task(operator())

    agent = Agent(
        name="concierge",
        model=ScriptedProvider(
            [
                call("ask_human", {"question": "Which city?"}),
                text("Booked in Kyoto."),
            ]
        ),
        tools=[ask_human(channel)],
    )
    result = await Runner.run(agent, "book something")
    assert result.output == "Booked in Kyoto."
    assert [q.question for q in seen] == ["Which city?"]

    channel.close()
    await asyncio.wait_for(op, timeout=1)  # iteration ended cleanly


async def test_questions_delivers_backlog_queued_before_iteration() -> None:
    channel = HumanChannel()
    q, fut = channel._new_question("early bird?")

    async def operator() -> None:
        async for question in channel.questions():
            channel.answer(question.id, "yes")

    op = asyncio.create_task(operator())
    assert await asyncio.wait_for(fut, timeout=1) == "yes"
    channel.close()
    await asyncio.wait_for(op, timeout=1)
    assert q.id not in channel._futures


async def test_resolved_while_queued_is_skipped() -> None:
    channel = HumanChannel()
    q, fut = channel._new_question("stale?")
    channel.cancel(q.id, "gone")
    with pytest.raises(ToolError):
        fut.result()

    seen: list[HumanQuestion] = []

    async def operator() -> None:
        async for question in channel.questions():
            seen.append(question)

    op = asyncio.create_task(operator())
    channel.close()
    await asyncio.wait_for(op, timeout=1)
    assert seen == []  # the cancelled question never reached the consumer


async def test_ask_after_close_fails_fast() -> None:
    channel = HumanChannel()
    channel.close()
    with pytest.raises(ToolError, match="closed"):
        channel._new_question("too late?")


async def test_questions_started_after_close_ends_immediately() -> None:
    # Nothing is ever enqueued after close(), so a late iterator must return
    # instead of awaiting a feed that can only stay silent forever.
    channel = HumanChannel()
    channel.close()

    async def consume_twice() -> int:
        seen = 0
        async for _ in channel.questions():  # consumes the close sentinel
            seen += 1
        async for _ in channel.questions():  # empty feed: must not hang
            seen += 1
        return seen

    assert await asyncio.wait_for(consume_twice(), timeout=1) == 0
