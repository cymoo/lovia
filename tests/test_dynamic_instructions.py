"""Tests for dynamic instructions: @agent.instruction + extra_instructions."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from lovia import Agent, Runner

from .scripted_provider import ScriptedProvider, text


@pytest.mark.asyncio
async def test_instruction_decorator_appends_fragment() -> None:
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="a", instructions="BASE", model=provider)

    @agent.instruction
    def add_tier(ctx) -> str:  # type: ignore[no-untyped-def]
        return "tier=gold"

    rendered = await agent.render_system_prompt(None)
    assert rendered == "BASE\n\ntier=gold"


@pytest.mark.asyncio
async def test_instruction_supports_async() -> None:
    agent = Agent(name="a", instructions="BASE")

    @agent.instruction
    async def addn(ctx) -> str:  # type: ignore[no-untyped-def]
        return "ASYNC"

    assert await agent.render_system_prompt(None) == "BASE\n\nASYNC"


@pytest.mark.asyncio
async def test_instruction_skips_empty_fragments() -> None:
    agent = Agent(name="a", instructions="BASE")

    @agent.instruction
    def empty(ctx) -> str:  # type: ignore[no-untyped-def]
        return ""

    @agent.instruction
    def good(ctx) -> str:  # type: ignore[no-untyped-def]
        return "GOOD"

    assert await agent.render_system_prompt(None) == "BASE\n\nGOOD"


@pytest.mark.asyncio
async def test_runner_extra_instructions_str() -> None:
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="a", instructions="BASE", model=provider)
    await Runner.run(agent, "hi", extra_instructions="Be concise.")
    sys_msg = provider.calls[0][0]
    assert sys_msg.role == "system"
    assert "BASE" in sys_msg.content
    assert "Be concise." in sys_msg.content


@pytest.mark.asyncio
async def test_render_system_prompt_combines_base_fragments_extra() -> None:
    agent = Agent(name="a", instructions="BASE")

    @agent.instruction
    def frag(ctx) -> str:  # type: ignore[no-untyped-def]
        return "FRAG"

    out = await agent.render_system_prompt(None, extra="EXTRA")
    assert out == "BASE\n\nFRAG\n\nEXTRA"


@pytest.mark.asyncio
async def test_clone_copies_fragments_independently() -> None:
    agent = Agent(name="a", instructions="BASE")

    @agent.instruction
    def f1(ctx) -> str:  # type: ignore[no-untyped-def]
        return "F1"

    twin = agent.clone(name="b")

    @twin.instruction
    def f2(ctx) -> str:  # type: ignore[no-untyped-def]
        return "F2"

    assert await agent.render_system_prompt(None) == "BASE\n\nF1"
    assert await twin.render_system_prompt(None) == "BASE\n\nF1\n\nF2"


@pytest.mark.asyncio
async def test_with_instructions_returns_clone() -> None:
    agent = Agent(name="a", instructions="BASE")

    def frag(ctx) -> str:  # type: ignore[no-untyped-def]
        return "FRAG"

    twin = agent.with_instructions(frag)

    assert await agent.render_system_prompt(None) == "BASE"
    assert await twin.render_system_prompt(None) == "BASE\n\nFRAG"


@pytest.mark.asyncio
async def test_instruction_fragment_receives_run_context() -> None:
    """A fragment gets the live RunContext (same handle tools/hooks receive)."""
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="a", instructions="BASE", model=provider)

    @agent.instruction
    def who(ctx) -> str:  # type: ignore[no-untyped-def]
        # Reach user deps via ctx.deps; ctx.turn is 0 (rendered pre-turn).
        return f"user={ctx.deps['name']} turn={ctx.turn}"

    await Runner.run(agent, "hi", context={"name": "Mei"})
    sys_msg = provider.calls[0][0]
    assert sys_msg.role == "system"
    assert "user=Mei turn=0" in sys_msg.content


class _Out(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_runner_output_type_override() -> None:
    """Override changes the parsed output type for a single run."""
    provider = ScriptedProvider([text('{"answer": "yes"}')])
    agent = Agent(name="a", model=provider)  # output_type=str by default
    result = await Runner.run(agent, "hi", output_type=_Out)
    assert isinstance(result.output, _Out)
    assert result.output.answer == "yes"


@pytest.mark.asyncio
async def test_runner_output_type_str_forces_text() -> None:
    """``output_type=str`` forces free-form text even if agent declares a model."""
    provider = ScriptedProvider([text("hello")])
    agent = Agent(name="a", model=provider, output_type=_Out)
    result = await Runner.run(agent, "hi", output_type=str)
    assert result.output == "hello"


@pytest.mark.asyncio
async def test_runner_output_type_none_uses_agent_default() -> None:
    provider = ScriptedProvider([text('{"answer": "ok"}')])
    agent = Agent(name="a", model=provider, output_type=_Out)
    result = await Runner.run(agent, "hi", output_type=None)
    assert isinstance(result.output, _Out)
