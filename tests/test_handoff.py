from __future__ import annotations

from lovia import Agent, Runner

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
