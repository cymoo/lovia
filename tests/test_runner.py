from __future__ import annotations

import pytest
from pydantic import BaseModel

from lovia import Agent, Runner, tool, events
from lovia.stores import InMemorySession
from lovia.exceptions import MaxTurnsExceeded

from .scripted_provider import ScriptedProvider, call, text


@tool
async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool
async def fail_tool() -> str:
    """A tool that always raises."""
    raise RuntimeError("boom")


async def test_plain_text_run() -> None:
    provider = ScriptedProvider([text("Hello!")])
    agent = Agent(name="t", instructions="be brief", model=provider)
    result = await Runner.run(agent, "hi")
    assert result.output == "Hello!"
    assert result.turns == 1
    # Initial messages: system + user; assistant reply is appended.
    assert [m.role for m in result.messages] == ["system", "user", "assistant"]


async def test_tool_call_round_trip() -> None:
    provider = ScriptedProvider(
        [
            call("add", {"a": 2, "b": 3}, call_id="c1"),
            text("The answer is 5."),
        ]
    )
    agent = Agent(name="t", instructions="be helpful", model=provider, tools=[add])
    result = await Runner.run(agent, "what is 2+3?")
    assert result.output == "The answer is 5."
    roles = [m.role for m in result.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    # Tool result message carries the call id.
    assert result.messages[3].tool_call_id == "c1"
    assert result.messages[3].content == "5"


async def test_unknown_tool_does_not_crash_run() -> None:
    provider = ScriptedProvider(
        [
            call("does_not_exist", {}, call_id="c1"),
            text("ok, gave up"),
        ]
    )
    agent = Agent(name="t", instructions="x", model=provider, tools=[add])
    result = await Runner.run(agent, "go")
    # The runner should record an error message and keep going.
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "not available" in tool_msg.content
    assert result.output == "ok, gave up"


async def test_tool_exception_is_reported() -> None:
    provider = ScriptedProvider(
        [
            call("fail_tool", {}, call_id="c1"),
            text("noted"),
        ]
    )
    agent = Agent(name="t", instructions="x", model=provider, tools=[fail_tool])
    result = await Runner.run(agent, "go")
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "boom" in tool_msg.content


async def test_structured_output_via_final_output_tool() -> None:
    class Sum(BaseModel):
        result: int

    # The scripted provider isn't an OpenAIChatProvider, so the runner will
    # fall back to the synthetic ``final_output`` tool.
    provider = ScriptedProvider(
        [
            call("final_output", {"result": 5}, call_id="c1"),
        ]
    )
    agent = Agent(name="t", model=provider, output_type=Sum)
    result = await Runner.run(agent, "what is 2+3?")
    assert isinstance(result.output, Sum)
    assert result.output.result == 5


async def test_max_turns_enforced() -> None:
    # Endless loop: model always calls a tool.
    provider = ScriptedProvider([call("add", {"a": 1, "b": 1}) for _ in range(5)])
    agent = Agent(name="t", model=provider, tools=[add])
    with pytest.raises(MaxTurnsExceeded):
        await Runner.run(agent, "go", max_turns=2)


async def test_typed_context_smoke() -> None:
    from dataclasses import dataclass

    @dataclass
    class Order:
        id: str
        qty: int

    agent: Agent[Order, str] = Agent(name="a", model=ScriptedProvider([text("ok")]))
    res = await Runner.run(agent, "ping", context=Order(id="o1", qty=1))
    assert res.output == "ok"


async def test_session_round_trip() -> None:
    sess = InMemorySession()
    provider = ScriptedProvider([text("hello, alice"), text("welcome back, alice")])

    agent = Agent(name="t", instructions="x", model=provider)
    r1 = await Runner.run(agent, "I'm Alice", session=sess, session_id="u-alice")
    assert r1.output == "hello, alice"

    r2 = await Runner.run(agent, "remember me?", session=sess, session_id="u-alice")
    assert r2.output == "welcome back, alice"
    # The second call should see the prior transcript.
    second_call_messages = provider.calls[1]
    roles = [m.role for m in second_call_messages]
    # system + (history: user + assistant) + new user
    assert roles == ["system", "user", "assistant", "user"]


async def test_streaming_yields_deltas_and_completion() -> None:
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider)
    seen: list[type] = []
    async for ev in Runner.run_stream(agent, "go"):
        seen.append(type(ev))
    assert events.RunStarted in seen
    assert events.TextDelta in seen
    assert events.MessageCompleted in seen
    assert events.RunCompleted in seen


# --------------------------------------------------------------------- B1 ----


async def test_run_streamed_handle_is_awaitable() -> None:
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider)
    handle = Runner.run_streamed(agent, "go")
    result = await handle
    assert result.output == "hi"


async def test_run_streamed_handle_is_iterable_then_result() -> None:
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider)
    handle = Runner.run_streamed(agent, "go")
    seen: list[type] = []
    async for ev in handle:
        seen.append(type(ev))
    assert events.RunCompleted in seen
    # ``result()`` must still return after iteration.
    result = await handle.result()
    assert result.output == "hi"


async def test_run_streamed_handle_double_iteration_raises() -> None:
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider)
    handle = Runner.run_streamed(agent, "go")
    async for _ in handle:
        pass
    with pytest.raises(RuntimeError):
        async for _ in handle:
            pass


# --------------------------------------------------------------------- B3 ----


async def test_handoff_input_filter_drops_stale_tool_calls() -> None:
    from lovia import Handoff, drop_stale_tool_calls

    specialist = Agent(name="specialist", model=ScriptedProvider([text("final")]))
    triage_provider = ScriptedProvider(
        [
            # Original agent first calls a tool, then hands off.
            call("add", {"a": 1, "b": 2}, call_id="c1"),
            call("transfer_to_specialist", {"reason": "spec"}, call_id="c2"),
        ]
    )
    triage = Agent(
        name="triage",
        model=triage_provider,
        tools=[add],
        handoffs=[Handoff(target=specialist, input_filter=drop_stale_tool_calls)],
    )
    # The specialist provider is shared via its agent.
    specialist.model = ScriptedProvider([text("final")])

    result = await Runner.run(triage, "go")
    assert result.output == "final"
    # The transcript the specialist saw must contain no tool messages.
    specialist_inbox = specialist.model.calls[0]  # type: ignore[attr-defined]
    assert all(m.role != "tool" for m in specialist_inbox)
    # And no assistant messages with dangling tool_calls.
    assert all(not (m.role == "assistant" and m.tool_calls) for m in specialist_inbox)


# --------------------------------------------------------------------- B4 ----


async def test_output_repair_recovers_from_invalid_json() -> None:
    class Sum(BaseModel):
        result: int

    provider = ScriptedProvider(
        [
            text("not even close to JSON"),
            text('{"result": 7}'),
        ]
    )
    agent = Agent(name="t", model=provider, output_type=Sum)
    result = await Runner.run(agent, "what?")
    assert isinstance(result.output, Sum)
    assert result.output.result == 7
    # The repair message must have been appended as a user turn between the
    # two assistant replies.
    second_call_messages = provider.calls[1]
    repair_msg = second_call_messages[-1]
    assert repair_msg.role == "user"
    assert "could not be parsed" in repair_msg.content


async def test_output_repair_disabled_raises() -> None:
    from lovia.exceptions import OutputValidationError

    class Sum(BaseModel):
        result: int

    provider = ScriptedProvider([text("garbage")])
    agent = Agent(name="t", model=provider, output_type=Sum, output_repair=False)
    with pytest.raises(OutputValidationError):
        await Runner.run(agent, "what?")


# --------------------------------------------------------------------- B5 ----


async def test_agent_as_tool_propagates_usage() -> None:
    from lovia import agent_as_tool

    child = Agent(name="child", model=ScriptedProvider([text("42")]))
    child_tool = agent_as_tool(child)

    parent_provider = ScriptedProvider(
        [
            call("ask_child", {"input": "what?"}, call_id="c1"),
            text("the answer is 42"),
        ]
    )
    parent = Agent(name="parent", model=parent_provider, tools=[child_tool])

    result = await Runner.run(parent, "go")
    # Parent + child token counts must accumulate (each scripted turn yields
    # ``Usage(input_tokens=1, output_tokens=1)``).
    assert result.usage.input_tokens >= 3
    assert result.usage.output_tokens >= 3
