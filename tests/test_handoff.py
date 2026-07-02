from __future__ import annotations

import pytest

from lovia import Agent, CancelToken, RunCancelled, RunContext, Runner, tool

from .scripted_provider import ScriptedProvider, call, text


async def test_handoff_switches_agents() -> None:
    spanish = Agent(
        name="Spanish",
        instructions="reply in Spanish",
        model=ScriptedProvider([text("Hola!")]),
    )
    # The English agent first hands off, then never speaks again because the
    # Spanish provider takes over.
    english = Agent(
        name="English",
        instructions="reply in English",
        model=ScriptedProvider(
            [call("transfer_to_spanish", {"reason": "user spoke Spanish"})]
        ),
        handoffs=[spanish],
    )
    result = await Runner.run(english, "Hola")
    assert result.final_agent.name == "Spanish"
    assert result.output == "Hola!"


async def test_agent_as_tool() -> None:
    expert = Agent(
        name="Expert",
        model=ScriptedProvider([text("42 is the answer")]),
    )
    # The parent calls the expert via a tool, then formats the reply.
    parent_provider = ScriptedProvider(
        [
            call("ask_expert", {"input": "what is the answer?"}, call_id="c1"),
            text("Got it: 42"),
        ]
    )
    parent = Agent(
        name="Parent",
        model=parent_provider,
        tools=[expert.as_tool()],
    )
    result = await Runner.run(parent, "delegate to expert")
    assert result.output == "Got it: 42"


async def test_agent_as_tool_forwards_max_turns() -> None:
    # The expert keeps calling a (missing) tool; only on its third turn would
    # it produce "done". max_turns=1 caps the sub-run before then, so the
    # sub-run raises MaxTurnsExceeded. The parent catches that as a tool error
    # and continues to its own next message.
    expert = Agent(
        name="Expert",
        model=ScriptedProvider([call("noop", {}), call("noop", {}), text("done")]),
    )
    tool = expert.as_tool(max_turns=1)
    parent = Agent(
        name="Parent",
        model=ScriptedProvider(
            [call(tool.name, {"input": "go"}, call_id="c1"), text("ok")]
        ),
        tools=[tool],
    )
    result = await Runner.run(parent, "delegate")
    # The expert's "done" is never reached; the parent recovers and answers.
    assert result.output == "ok"


async def test_agent_as_tool_inherits_cancel_token() -> None:
    # The sub-run must see the *same* token instance the parent run was given,
    # so a cancel() can reach it while the parent is blocked awaiting the child.
    token = CancelToken()
    seen: list[CancelToken] = []

    @tool
    async def record(ctx: RunContext) -> str:
        seen.append(ctx.cancel_token)
        return "noted"

    child = Agent(
        name="Child",
        model=ScriptedProvider([call("record", {}), text("child done")]),
        tools=[record],
    )
    parent = Agent(
        name="Parent",
        model=ScriptedProvider(
            [call("ask_child", {"input": "go"}, call_id="c1"), text("ok")]
        ),
        tools=[child.as_tool()],
    )
    result = await Runner.run(parent, "delegate", cancel_token=token)
    assert result.output == "ok"
    assert seen == [token]  # the child's tool saw the parent's exact token


async def test_cancel_inside_sub_run_terminates_parent() -> None:
    # A cancel issued from within the sub-run (via the inherited token) must
    # propagate up and terminate the parent run — not be swallowed into a
    # tool-error result the way an arbitrary tool exception would be.
    @tool
    async def stop(ctx: RunContext) -> str:
        ctx.cancel_token.cancel("child decided to stop")
        return "stopping"

    child = Agent(
        name="Child",
        model=ScriptedProvider([call("stop", {}), text("never reached")]),
        tools=[stop],
    )
    parent = Agent(
        name="Parent",
        model=ScriptedProvider(
            [call("ask_child", {"input": "go"}, call_id="c1"), text("never reached")]
        ),
        tools=[child.as_tool()],
    )
    with pytest.raises(RunCancelled):
        await Runner.run(parent, "delegate")
