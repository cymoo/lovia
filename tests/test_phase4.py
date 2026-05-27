"""Phase-4 tests: checkpointer + permission literals."""

from __future__ import annotations

from typing import Any

import pytest

from lovia import (
    Agent,
    InMemoryCheckpointer,
    Runner,
    RunSnapshot,
    events,
    tool,
)
from lovia.messages import ChatMessage, ToolCall, Usage
from lovia.stores.sqlite_checkpointer import SQLiteCheckpointer

from .scripted_provider import ScriptedProvider, call, text


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
    assert any(m.role == "assistant" for m in snap.messages)
    assert snap.usage.output_tokens > 0


@pytest.mark.asyncio
async def test_resume_continues_from_snapshot() -> None:
    # Pre-seed a snapshot: a transcript containing user prompt + an assistant
    # message that issues a tool call, plus the tool result. Resume should
    # pick up the next turn (calling provider for a final answer).
    cp = InMemoryCheckpointer()
    transcript = [
        ChatMessage(role="user", content="What is the time?"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="c1", name="clock", arguments="{}")],
        ),
        ChatMessage(role="tool", tool_call_id="c1", content="12:00"),
    ]
    await cp.save(
        RunSnapshot(
            run_id="r2",
            agent_name="a",
            messages=transcript,
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
    # Resumed transcript starts with the saved 3 messages.
    assert result.messages[:3] == transcript
    # And usage carries forward.
    assert result.usage.input_tokens >= 10


@pytest.mark.asyncio
async def test_resume_missing_run_id_raises() -> None:
    cp = InMemoryCheckpointer()
    agent = Agent(name="a", model=ScriptedProvider([]))
    with pytest.raises(Exception, match="No snapshot"):
        await Runner.resume(agent, checkpointer=cp, run_id="missing")


@pytest.mark.asyncio
async def test_sqlite_checkpointer_persists(tmp_path: Any) -> None:
    db = tmp_path / "ckpt.sqlite"
    cp = SQLiteCheckpointer(db)
    provider = ScriptedProvider([text("persisted")])
    agent = Agent(name="a", model=provider)
    await Runner.run(agent, "hi", checkpointer=cp, run_id="r3")

    # New instance, same file: snapshot survives.
    cp2 = SQLiteCheckpointer(db)
    snap = await cp2.load("r3")
    assert snap is not None and snap.agent_name == "a"
    await cp2.delete("r3")
    assert await cp2.load("r3") is None


@pytest.mark.asyncio
async def test_approval_handler_literal_allow_and_deny() -> None:
    calls_made: list[str] = []

    @tool(needs_approval=True)
    async def dangerous() -> str:
        calls_made.append("ran")
        return "ok"

    # "allow" → tool runs
    provider1 = ScriptedProvider([call("dangerous", {}), text("done")])
    agent1 = Agent(
        name="a",
        model=provider1,
        tools=[dangerous],
        approval_handler=lambda c, ctx: "allow",
    )
    r1 = await Runner.run(agent1, "go")
    assert r1.output == "done"
    assert calls_made == ["ran"]

    # "deny" → tool blocked
    calls_made.clear()
    provider2 = ScriptedProvider([call("dangerous", {}), text("ack")])
    agent2 = Agent(
        name="a",
        model=provider2,
        tools=[dangerous],
        approval_handler=lambda c, ctx: "deny",
    )
    r2 = await Runner.run(agent2, "go")
    assert calls_made == []
    assert "not approved" in next(m.content for m in r2.messages if m.role == "tool")


@pytest.mark.asyncio
async def test_approval_handler_ask_defers_to_streaming_consumer() -> None:
    @tool(needs_approval=True)
    async def dangerous() -> str:
        return "ok"

    provider = ScriptedProvider([call("dangerous", {}), text("done")])
    agent = Agent(
        name="a",
        model=provider,
        tools=[dangerous],
        approval_handler=lambda c, ctx: "ask",
    )
    handle = Runner.run_streamed(agent, "go")
    async for event in handle:
        if isinstance(event, events.ApprovalRequired):
            event.approve()  # streaming consumer resolves
    result = await handle.result()
    assert result.output == "done"
