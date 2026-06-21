"""Tests for dynamic instructions: @agent.instruction + extra_instructions."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from lovia import Agent, RunContext, Runner, tool

from .scripted_provider import ScriptedProvider, call, text


def _ctx(agent: Agent) -> RunContext:
    """A minimal RunContext for direct ``render_system_prompt()`` calls.

    ``render_system_prompt`` now takes the same handle the runner passes, so the
    unit tests exercise the real signature instead of ``None``.
    """
    return RunContext(context=None, entries=[], agent=agent)


@pytest.mark.asyncio
async def test_instruction_decorator_appends_fragment() -> None:
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="a", instructions="BASE", model=provider)

    @agent.instruction
    def add_tier(ctx) -> str:  # type: ignore[no-untyped-def]
        return "tier=gold"

    rendered = await agent.render_system_prompt(_ctx(agent))
    assert rendered == "BASE\n\ntier=gold"


@pytest.mark.asyncio
async def test_instruction_supports_async() -> None:
    agent = Agent(name="a", instructions="BASE")

    @agent.instruction
    async def addn(ctx) -> str:  # type: ignore[no-untyped-def]
        return "ASYNC"

    assert await agent.render_system_prompt(_ctx(agent)) == "BASE\n\nASYNC"


@pytest.mark.asyncio
async def test_instruction_skips_empty_fragments() -> None:
    agent = Agent(name="a", instructions="BASE")

    @agent.instruction
    def empty(ctx) -> str:  # type: ignore[no-untyped-def]
        return ""

    @agent.instruction
    def good(ctx) -> str:  # type: ignore[no-untyped-def]
        return "GOOD"

    assert await agent.render_system_prompt(_ctx(agent)) == "BASE\n\nGOOD"


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

    out = await agent.render_system_prompt(_ctx(agent), extra="EXTRA")
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

    assert await agent.render_system_prompt(_ctx(agent)) == "BASE\n\nF1"
    assert await twin.render_system_prompt(_ctx(twin)) == "BASE\n\nF1\n\nF2"


@pytest.mark.asyncio
async def test_with_instructions_returns_clone() -> None:
    agent = Agent(name="a", instructions="BASE")

    def frag(ctx) -> str:  # type: ignore[no-untyped-def]
        return "FRAG"

    twin = agent.with_instructions(frag)

    assert await agent.render_system_prompt(_ctx(agent)) == "BASE"
    assert await twin.render_system_prompt(_ctx(twin)) == "BASE\n\nFRAG"


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


@pytest.mark.asyncio
async def test_compaction_reuses_stored_system_entry() -> None:
    """When compaction drops the system head, the view re-prepends the stored
    entry (rendered once at turn 0) instead of re-running fragments at a later
    turn — so every turn's provider input carries the same system text."""
    from lovia.context import CompactionRequest, ContextResult
    from lovia.transcript import InputEntry

    class DropSystem:
        name = "drop-system"

        async def compact(self, req: CompactionRequest) -> ContextResult:
            body = [
                e
                for e in req.entries
                if not (isinstance(e, InputEntry) and e.role == "system")
            ]
            return ContextResult(entries=body, changed=True)

    @tool
    def noop() -> str:
        return "ok"

    # Turn 1 calls a tool, turn 2 answers — so _build_view runs on a turn where
    # ctx.turn has advanced past 0.
    provider = ScriptedProvider([call("noop", {}), text("done")])
    agent = Agent(name="a", instructions="BASE", model=provider, tools=[noop])

    @agent.instruction
    def stamp(ctx) -> str:  # type: ignore[no-untyped-def]
        return f"render_turn={ctx.turn}"

    await Runner.run(agent, "go", context_policy=DropSystem())

    # Every turn saw a system message, all carrying the turn-0 render — not a
    # per-turn re-render (which would read render_turn=1, render_turn=2, ...).
    assert len(provider.calls) == 2
    for messages in provider.calls:
        assert messages[0].role == "system"
        assert "render_turn=0" in messages[0].content


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
