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


@pytest.mark.asyncio
async def test_raising_approval_predicate_denies_without_dangling_call() -> None:
    # A needs_approval *predicate* that raises must fail closed (deny) and
    # still append a ToolResultEntry — a crash here would leave a dangling
    # tool call that a resume would then re-execute.
    from lovia.transcript import ToolCallEntry, ToolResultEntry

    executed: list[int] = []

    def exploding_predicate(_args, _ctx):  # type: ignore[no-untyped-def]
        raise ValueError("predicate exploded")

    @tool(needs_approval=exploding_predicate)
    async def guarded() -> str:
        executed.append(1)
        return "ran"

    provider = ScriptedProvider([call("guarded", {}, call_id="g1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[guarded])

    handle = Runner.stream(agent, "go")
    errors: list[BaseException] = []
    async for ev in handle:
        if isinstance(ev, events.ErrorOccurred):
            errors.append(ev.error)
    result = await handle.result()

    assert result.output == "done"  # the run survives
    assert executed == []  # fail closed: the tool never ran
    assert any(isinstance(e, ValueError) for e in errors)
    calls = [e for e in result.entries if isinstance(e, ToolCallEntry)]
    results = [e for e in result.entries if isinstance(e, ToolResultEntry)]
    assert len(calls) == len(results) == 1  # no dangling call
    assert results[0].is_error
    assert "not approved" in results[0].output


@pytest.mark.asyncio
async def test_raising_result_renderer_becomes_error_result() -> None:
    # The tool already ran when rendering fails; crashing the run would leave
    # a dangling call and re-execute the (possibly non-idempotent) tool on
    # resume. Instead the failure becomes an error tool-result.
    from lovia.transcript import ToolCallEntry, ToolResultEntry

    executions: list[int] = []

    def exploding_renderer(_value, _ctx):  # type: ignore[no-untyped-def]
        raise RuntimeError("renderer exploded")

    @tool(result_renderer=exploding_renderer)
    async def pay() -> str:
        executions.append(1)
        return "charged $100"

    provider = ScriptedProvider([call("pay", {}, call_id="p1"), text("done")])
    agent = Agent(name="t", model=provider, tools=[pay])

    handle = Runner.stream(agent, "go")
    errors: list[BaseException] = []
    async for ev in handle:
        if isinstance(ev, events.ErrorOccurred):
            errors.append(ev.error)
    result = await handle.result()

    assert result.output == "done"
    assert executions == [1]  # ran exactly once; the run was not re-driven
    assert any(isinstance(e, RuntimeError) for e in errors)
    calls = [e for e in result.entries if isinstance(e, ToolCallEntry)]
    results = [e for e in result.entries if isinstance(e, ToolResultEntry)]
    assert len(calls) == len(results) == 1  # no dangling call
    assert results[0].is_error
    assert "rendering failed" in results[0].output
