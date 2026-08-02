"""``HumanChannel.questions()`` — the push-based consumer loop."""

from __future__ import annotations

import asyncio

import pytest

from lovia import Agent, Runner
from lovia.exceptions import InvalidToolArguments, ToolError
from lovia.run_context import RunContext
from lovia.testing import ScriptedProvider, call, text
from lovia.tools.base import run_tool
from lovia.tools.human import HumanChannel, HumanQuestion, QuestionOption, ask_human


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


async def test_options_flow_through_to_the_operator() -> None:
    channel = HumanChannel()
    seen: list[HumanQuestion] = []

    async def operator() -> None:
        async for q in channel.questions():
            seen.append(q)
            channel.answer(q.id, ", ".join(o.label for o in q.options))

    op = asyncio.create_task(operator())

    agent = Agent(
        name="concierge",
        model=ScriptedProvider(
            [
                call(
                    "ask_human",
                    {
                        "question": "Which cities?",
                        "options": [
                            {"label": "Kyoto", "description": "temples"},
                            {"label": "Osaka"},
                        ],
                        "multi_select": True,
                    },
                ),
                text("Kyoto and Osaka it is."),
            ]
        ),
        tools=[ask_human(channel)],
    )
    result = await Runner.run(agent, "plan a trip")
    assert result.output == "Kyoto and Osaka it is."

    (q,) = seen
    assert [o.label for o in q.options] == ["Kyoto", "Osaka"]
    assert q.options[0].description == "temples"
    assert q.options[1].description == ""
    assert q.multi_select is True

    channel.close()
    await asyncio.wait_for(op, timeout=1)


async def test_question_without_options_stays_free_form() -> None:
    channel = HumanChannel()
    q, fut = channel._new_question("anything?")
    assert q.options == []
    assert q.multi_select is False
    assert q.session_id is None and q.run_id is None
    channel.answer(q.id, "sure")
    assert await asyncio.wait_for(fut, timeout=1) == "sure"


async def test_question_carries_session_and_run_identity() -> None:
    channel = HumanChannel()
    the_tool = ask_human(channel)
    agent: Agent[None] = Agent(name="x", model=ScriptedProvider([]))
    ctx: RunContext[None] = RunContext(
        context=None, entries=[], agent=agent, session_id="s1", run_id="r1"
    )

    async def operator() -> None:
        async for q in channel.questions():
            assert (q.session_id, q.run_id) == ("s1", "r1")
            channel.answer(q.id, "ok")
            channel.close()

    op = asyncio.create_task(operator())
    answer = await run_tool(the_tool, {"question": "who am I?"}, ctx)
    assert answer == "ok"
    await asyncio.wait_for(op, timeout=1)


async def test_option_count_is_schema_enforced() -> None:
    channel = HumanChannel()
    the_tool = ask_human(channel)
    agent: Agent[None] = Agent(name="x", model=ScriptedProvider([]))
    ctx: RunContext[None] = RunContext(context=None, entries=[], agent=agent)

    for bad in (
        [{"label": "only one"}],
        [{"label": f"o{i}"} for i in range(5)],
        [{"label": "twin"}, {"label": "twin"}],  # duplicate labels
        [{"label": "one\ntwo"}, {"label": "three"}],  # multi-line label
        [{"label": ""}, {"label": "ok"}],  # empty label
    ):
        with pytest.raises(InvalidToolArguments):
            await run_tool(the_tool, {"question": "pick", "options": bad}, ctx)
    assert channel.pending == []  # rejected calls never became questions


def test_ask_human_is_an_execution_barrier() -> None:
    # One pending question per run at a time: answers often steer what comes
    # next, so ask_human must not run concurrently with other tools.
    assert ask_human(HumanChannel()).parallel is False


def test_option_labels_round_trip_as_models() -> None:
    o = QuestionOption(label="Ship it", description="merge and release now")
    assert (o.label, o.description) == ("Ship it", "merge and release now")


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
