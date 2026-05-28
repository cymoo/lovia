"""Tests for tool approval flows: streaming events, handlers, literal verdicts."""

from __future__ import annotations

import pytest

from lovia import Agent, Runner, events, tool
from lovia.approvals import ApprovalChannel

from .scripted_provider import ScriptedProvider, call, text


@tool(needs_approval=True)
async def sensitive() -> str:
    """A sensitive tool."""
    return "did it"


# ---------- Streaming-event approval ----------


@pytest.mark.asyncio
async def test_streaming_approve_allows_tool() -> None:
    provider = ScriptedProvider([call("sensitive", {}, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[sensitive])
    handle = Runner.stream(agent, "go")
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            ev.approve()
    result = await handle.result()
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == "did it"


@pytest.mark.asyncio
async def test_streaming_reject_blocks_tool() -> None:
    provider = ScriptedProvider(
        [call("sensitive", {}, call_id="c1"), text("understood")]
    )
    agent = Agent(name="t", model=provider, tools=[sensitive])
    handle = Runner.stream(agent, "go")
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            ev.reject()
    result = await handle.result()
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "not approved" in tool_msg.content


# ---------- Handler returning bool ----------


@pytest.mark.asyncio
async def test_handler_returning_true_allows() -> None:
    provider = ScriptedProvider([call("sensitive", {}, call_id="c1"), text("done")])

    async def allow_all(_call, _ctx):  # type: ignore[no-untyped-def]
        return True

    agent = Agent(
        name="t", model=provider, tools=[sensitive], approval_handler=allow_all
    )
    result = await Runner.run(agent, "go")
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == "did it"


@pytest.mark.asyncio
async def test_no_handler_in_non_streaming_run_default_denies() -> None:
    provider = ScriptedProvider([call("sensitive", {}, call_id="c1"), text("ok")])
    agent = Agent(name="t", model=provider, tools=[sensitive])
    result = await Runner.run(agent, "go")
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "not approved" in tool_msg.content


# ---------- Handler returning literal verdicts ----------


@pytest.mark.asyncio
async def test_handler_literal_allow_runs_tool() -> None:
    ran: list[str] = []

    @tool(needs_approval=True)
    async def dangerous() -> str:
        ran.append("yes")
        return "ok"

    provider = ScriptedProvider([call("dangerous", {}), text("done")])
    agent = Agent(
        name="a",
        model=provider,
        tools=[dangerous],
        approval_handler=lambda c, ctx: "allow",
    )
    await Runner.run(agent, "go")
    assert ran == ["yes"]


@pytest.mark.asyncio
async def test_handler_literal_deny_blocks_tool() -> None:
    ran: list[str] = []

    @tool(needs_approval=True)
    async def dangerous() -> str:  # pragma: no cover
        ran.append("yes")
        return "ok"

    provider = ScriptedProvider([call("dangerous", {}), text("ack")])
    agent = Agent(
        name="a",
        model=provider,
        tools=[dangerous],
        approval_handler=lambda c, ctx: "deny",
    )
    result = await Runner.run(agent, "go")
    assert ran == []
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "not approved" in tool_msg.content


@pytest.mark.asyncio
async def test_handler_literal_ask_defers_to_streaming_consumer() -> None:
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
    handle = Runner.stream(agent, "go")
    async for event in handle:
        if isinstance(event, events.ApprovalRequired):
            event.approve()
    result = await handle.result()
    assert result.output == "done"


# ---------- Out-of-band ApprovalChannel ----------


@pytest.mark.asyncio
async def test_approval_channel_resolves_by_call_id() -> None:
    """A caller outside the event stream can approve via handle.approvals."""
    provider = ScriptedProvider([call("sensitive", {}, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[sensitive])
    handle = Runner.stream(agent, "go")
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            # Resolve via the channel rather than ev.approve().
            handle.approvals.approve(ev.call.id)
    result = await handle.result()
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == "did it"


@pytest.mark.asyncio
async def test_approval_channel_supports_scopes() -> None:
    channel = ApprovalChannel()
    fut_a = channel.register("c1", scope="a")
    fut_b = channel.register("c1", scope="b")

    assert channel.resolve("c1", True, scope="a") is True
    assert fut_a.result() is True
    assert not fut_b.done()

    channel.release(scope="b")
    assert fut_b.result() is False
