"""Tests for tool approval flows: streaming events, handlers, literal verdicts."""

from __future__ import annotations

import pytest

from lovia import Agent, Approval, Runner, Verdict, events, tool
from lovia.approvals import ApprovalChannel, as_verdict, merge_verdicts

from .scripted_provider import ScriptedProvider, call, text


@tool(permission="ask")
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
    assert tool_msg.content == "Tool sensitive was not approved."


@pytest.mark.asyncio
async def test_streaming_reject_with_reason_reaches_the_model() -> None:
    provider = ScriptedProvider([call("sensitive", {}, call_id="c1"), text("ok")])
    agent = Agent(name="t", model=provider, tools=[sensitive])
    handle = Runner.stream(agent, "go")
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            ev.reject("not on a Friday; open a PR instead")
    result = await handle.result()
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == (
        "Tool sensitive was not approved: not on a Friday; open a PR instead"
    )


# ---------- Verdicts: constants and predicates ----------


@pytest.mark.asyncio
async def test_static_verdict_puts_reason_and_scope_on_the_event() -> None:
    @tool(permission=Verdict("ask", "Sends mail outside the company.", "mail:external"))
    async def send() -> str:
        return "sent"

    provider = ScriptedProvider([call("send", {}, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[send])
    handle = Runner.stream(agent, "go")
    seen: list[events.ApprovalRequired] = []
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            seen.append(ev)
            ev.approve()
    await handle.result()
    assert len(seen) == 1
    assert seen[0].reason == "Sends mail outside the company."
    assert seen[0].scope == "mail:external"


@pytest.mark.asyncio
async def test_predicate_deny_is_refused_with_its_reason_before_running() -> None:
    ran: list[int] = []

    def policy(args, ctx):  # type: ignore[no-untyped-def]
        if args["amount_cents"] > 1_000_000:
            return Verdict("deny", "refunds above 10,000 USD go through finance")
        return "allow"

    @tool(permission=policy)
    async def refund(amount_cents: int) -> str:
        ran.append(amount_cents)
        return "refunded"

    provider = ScriptedProvider(
        [call("refund", {"amount_cents": 5_000_000}, call_id="c1"), text("ok")]
    )
    agent = Agent(name="t", model=provider, tools=[refund])
    handle = Runner.stream(agent, "go")
    asked = False
    async for ev in handle:
        asked = asked or isinstance(ev, events.ApprovalRequired)
    result = await handle.result()
    assert ran == [] and not asked  # deny never reaches a human
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == (
        "Tool refund was denied by policy: refunds above 10,000 USD go through finance"
    )


def test_as_verdict_accepts_literals_and_verdicts_only() -> None:
    assert as_verdict("ask") == Verdict("ask")
    assert as_verdict(Verdict("deny", "no")) == Verdict("deny", "no")
    with pytest.raises(TypeError):
        as_verdict(True)
    with pytest.raises(TypeError):
        as_verdict("maybe")


def test_merge_verdicts_keeps_scope_only_for_a_sole_ask() -> None:
    assert merge_verdicts(Verdict("allow"), Verdict("ask", "a", "x")).scope == "x"
    assert (
        merge_verdicts(Verdict("ask", "a", "x"), Verdict("ask", "b", "y")).scope is None
    )
    assert (
        merge_verdicts(Verdict("ask", "a", "x"), Verdict("deny", "d")).action == "deny"
    )


# ---------- Handler returning bool ----------


@pytest.mark.asyncio
async def test_handler_returning_true_allows() -> None:
    provider = ScriptedProvider([call("sensitive", {}, call_id="c1"), text("done")])

    async def allow_all(_request, _ctx):  # type: ignore[no-untyped-def]
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
    assert tool_msg.content == "Tool sensitive was not approved: no approver available"


# ---------- Handler receives the request; literal and Approval verdicts ----------


@pytest.mark.asyncio
async def test_handler_sees_the_request_and_may_answer_with_a_reason() -> None:
    seen: list[events.ApprovalRequired] = []

    def handler(request, ctx):  # type: ignore[no-untyped-def]
        seen.append(request)
        return Approval(False, "handler says no")

    @tool(permission=Verdict("ask", "why", "scope:x"))
    async def dangerous() -> str:  # pragma: no cover
        return "ok"

    provider = ScriptedProvider([call("dangerous", {}), text("ack")])
    agent = Agent(name="a", model=provider, tools=[dangerous], approval_handler=handler)
    result = await Runner.run(agent, "go")
    assert seen[0].call.name == "dangerous"
    assert seen[0].reason == "why" and seen[0].scope == "scope:x"
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == "Tool dangerous was not approved: handler says no"


@pytest.mark.asyncio
async def test_handler_literal_allow_runs_tool() -> None:
    ran: list[str] = []

    @tool(permission="ask")
    async def dangerous() -> str:
        ran.append("yes")
        return "ok"

    provider = ScriptedProvider([call("dangerous", {}), text("done")])
    agent = Agent(
        name="a",
        model=provider,
        tools=[dangerous],
        approval_handler=lambda req, ctx: "allow",
    )
    await Runner.run(agent, "go")
    assert ran == ["yes"]


@pytest.mark.asyncio
async def test_handler_literal_deny_blocks_tool() -> None:
    ran: list[str] = []

    @tool(permission="ask")
    async def dangerous() -> str:  # pragma: no cover
        ran.append("yes")
        return "ok"

    provider = ScriptedProvider([call("dangerous", {}), text("ack")])
    agent = Agent(
        name="a",
        model=provider,
        tools=[dangerous],
        approval_handler=lambda req, ctx: "deny",
    )
    result = await Runner.run(agent, "go")
    assert ran == []
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == "Tool dangerous was not approved."


@pytest.mark.asyncio
async def test_handler_invalid_decision_denies_with_reason() -> None:
    @tool(permission="ask")
    async def dangerous() -> str:  # pragma: no cover
        return "ok"

    provider = ScriptedProvider([call("dangerous", {}), text("ack")])
    agent = Agent(
        name="a",
        model=provider,
        tools=[dangerous],
        approval_handler=lambda req, ctx: "maybe",  # type: ignore[arg-type,return-value]
    )
    result = await Runner.run(agent, "go")
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "invalid decision" in tool_msg.content


@pytest.mark.asyncio
async def test_handler_literal_ask_defers_to_streaming_consumer() -> None:
    @tool(permission="ask")
    async def dangerous() -> str:
        return "ok"

    provider = ScriptedProvider([call("dangerous", {}), text("done")])
    agent = Agent(
        name="a",
        model=provider,
        tools=[dangerous],
        approval_handler=lambda req, ctx: "ask",
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
async def test_approval_channel_supports_scopes_and_reasons() -> None:
    channel = ApprovalChannel()
    fut_a = channel.register("c1", scope="a")
    fut_b = channel.register("c1", scope="b")

    assert channel.resolve("c1", True, scope="a") is True
    assert fut_a.result() == Approval(True)
    assert not fut_b.done()

    channel.release(scope="b", reason="the run ended")
    assert fut_b.result() == Approval(False, "the run ended")
