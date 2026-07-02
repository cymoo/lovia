"""Runtime tool-phase edge cases not covered by tests/test_approval.py.

Specifically the fail-closed corners of the approval gate:

* the ``approval_handler`` itself raising — must be logged, surfaced as an
  ``ErrorOccurred`` event, and treated as a denial;
* a handler returning ``"ask"`` in a *non-streaming* run, where there is no
  consumer to resolve the request, so it must fall through to default-deny.
"""

from __future__ import annotations

import pytest

from lovia import Agent, Runner, events, tool

from ..scripted_provider import ScriptedProvider, call, text


@tool(needs_approval=True)
async def sensitive() -> str:
    """A sensitive tool that must be approved before it runs."""
    return "did it"


@pytest.mark.asyncio
async def test_raising_handler_is_denied_and_surfaces_error_event() -> None:
    provider = ScriptedProvider(
        [call("sensitive", {}, call_id="c1"), text("understood")]
    )

    def boom(_call, _ctx):  # type: ignore[no-untyped-def]
        raise RuntimeError("handler crashed")

    agent = Agent(name="t", model=provider, tools=[sensitive], approval_handler=boom)

    handle = Runner.stream(agent, "go")
    errors: list[BaseException] = []
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            pass  # deliberately do NOT resolve -> handler gets consulted
        elif isinstance(ev, events.ErrorOccurred):
            errors.append(ev.error)
    result = await handle.result()

    assert any(isinstance(e, RuntimeError) for e in errors)
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "not approved" in tool_msg.content


@pytest.mark.asyncio
async def test_ask_in_non_streaming_run_defaults_to_deny() -> None:
    # "ask" defers to a streaming consumer; in a plain run() nobody decides,
    # so the fail-closed default must deny the call.
    provider = ScriptedProvider([call("sensitive", {}, call_id="c1"), text("ack")])
    agent = Agent(
        name="t",
        model=provider,
        tools=[sensitive],
        approval_handler=lambda c, ctx: "ask",
    )
    result = await Runner.run(agent, "go")
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "not approved" in tool_msg.content
