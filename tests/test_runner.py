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
