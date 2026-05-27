"""Tests for the checkpointer protocol and snapshot round-tripping."""

from __future__ import annotations

from typing import Any

import pytest

from lovia import (
    Agent,
    ImageBlock,
    InMemoryCheckpointer,
    InputMessageItem,
    MessageOutputItem,
    Runner,
    RunSnapshot,
    TextBlock,
    ToolCallItem,
    ToolCallOutputItem,
    tool,
)
from lovia.messages import Usage
from lovia.stores.sqlite_checkpointer import SQLiteCheckpointer

from .scripted_provider import ScriptedProvider, text


@pytest.mark.asyncio
async def test_checkpointer_snapshot_round_trip() -> None:
    cp = InMemoryCheckpointer()
    provider = ScriptedProvider([text("hello there")])
    agent = Agent(name="a", model=provider)
    result = await Runner.run(agent, "hi", checkpointer=cp, run_id="r1")
    assert result.output == "hello there"

    snap = await cp.load("r1")
    assert snap is not None
    assert snap.run_id == "r1"
    assert snap.agent_name == "a"
    # The assistant turn shows up as a MessageOutputItem in the snapshot.
    assert any(isinstance(it, MessageOutputItem) for it in snap.items)
    assert snap.usage.output_tokens > 0


@pytest.mark.asyncio
async def test_resume_continues_from_snapshot() -> None:
    cp = InMemoryCheckpointer()
    items = [
        InputMessageItem(role="user", content="What is the time?"),
        ToolCallItem(call_id="c1", name="clock", arguments="{}"),
        ToolCallOutputItem(call_id="c1", output="12:00"),
    ]
    await cp.save(
        RunSnapshot(
            run_id="r2",
            agent_name="a",
            items=items,
            usage=Usage(input_tokens=10, output_tokens=5),
            turns=1,
        )
    )

    @tool
    async def clock() -> str:
        return "12:00"

    provider = ScriptedProvider([text("It is noon.")])
    agent = Agent(name="a", model=provider, tools=[clock])

    result = await Runner.resume(agent, checkpointer=cp, run_id="r2")
    assert result.output == "It is noon."
    # The first three items survive the resume verbatim.
    assert result.new_items[:3] == items
    assert result.usage.input_tokens >= 10


@pytest.mark.asyncio
async def test_resume_missing_run_id_raises() -> None:
    cp = InMemoryCheckpointer()
    agent = Agent(name="a", model=ScriptedProvider([]))
    with pytest.raises(Exception, match="No snapshot"):
        await Runner.resume(agent, checkpointer=cp, run_id="missing")


@pytest.mark.asyncio
async def test_sqlite_checkpointer_persists_across_instances(tmp_path: Any) -> None:
    db = tmp_path / "ckpt.sqlite"
    cp = SQLiteCheckpointer(db)
    provider = ScriptedProvider([text("persisted")])
    agent = Agent(name="a", model=provider)
    await Runner.run(agent, "hi", checkpointer=cp, run_id="r3")

    cp2 = SQLiteCheckpointer(db)
    snap = await cp2.load("r3")
    assert snap is not None and snap.agent_name == "a"


@pytest.mark.asyncio
async def test_sqlite_checkpointer_delete_is_idempotent(tmp_path: Any) -> None:
    cp = SQLiteCheckpointer(tmp_path / "ckpt.sqlite")
    provider = ScriptedProvider([text("x")])
    agent = Agent(name="a", model=provider)
    await Runner.run(agent, "hi", checkpointer=cp, run_id="r")
    await cp.delete("r")
    await cp.delete("r")  # second delete must not raise
    assert await cp.load("r") is None


def test_snapshot_to_dict_round_trip_preserves_multimodal_content() -> None:
    snap = RunSnapshot(
        run_id="r",
        agent_name="a",
        items=[
            InputMessageItem(
                role="user",
                content=[TextBlock("describe this"), ImageBlock(url="https://x/y.png")],
            ),
            MessageOutputItem(content="a cat"),
        ],
        usage=Usage(input_tokens=3, output_tokens=2, cache_read_tokens=1),
        turns=1,
    )
    payload = snap.to_dict()
    restored = RunSnapshot.from_dict(payload)
    assert restored.run_id == snap.run_id
    assert restored.usage.cache_read_tokens == 1
    first = restored.items[0]
    assert isinstance(first, InputMessageItem)
    assert isinstance(first.content, list)
    assert isinstance(first.content[0], TextBlock)
    assert isinstance(first.content[1], ImageBlock)
    assert first.content[1].url == "https://x/y.png"
