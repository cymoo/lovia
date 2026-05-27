"""Tests for OutputRepairStrategy and agent-level tool_result_renderer."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lovia import (
    Agent,
    DefaultOutputRepair,
    OutputValidationError,
    Runner,
    tool,
)

from .scripted_provider import ScriptedProvider, call, text


# ---------- OutputRepairStrategy ----------


@dataclass
class Counting:
    """A repair strategy that counts attempts and caps at ``cap``."""

    cap: int = 2
    seen: int = 0

    def build_prompt(self, exc: OutputValidationError, attempt: int) -> str | None:
        self.seen = attempt
        if attempt > self.cap:
            return None
        return f"Retry #{attempt}: please return valid JSON only."


@dataclass
class Out:
    n: int


@pytest.mark.asyncio
async def test_custom_repair_strategy_retries_until_cap() -> None:
    """Custom strategy is consulted on every failure until it returns None."""
    # First two outputs are bad, third is valid.
    provider = ScriptedProvider(
        [text("not json"), text("still not json"), text('{"n": 7}')]
    )
    strategy = Counting(cap=2)
    agent = Agent(
        name="r",
        model=provider,
        output_type=Out,
        output_repair=strategy,
    )
    result = await Runner.run(agent, "go")
    assert result.output == Out(n=7)
    # Two repair prompts were issued before success.
    assert strategy.seen == 2


@pytest.mark.asyncio
async def test_default_repair_caps_at_one_attempt() -> None:
    """The default ``output_repair=True`` matches DefaultOutputRepair()."""
    provider = ScriptedProvider([text("bad"), text("still bad"), text("never reached")])
    agent = Agent(name="r", model=provider, output_type=Out)
    with pytest.raises(OutputValidationError):
        await Runner.run(agent, "go")


@pytest.mark.asyncio
async def test_repair_false_fails_immediately() -> None:
    """``output_repair=False`` disables retries entirely."""
    provider = ScriptedProvider([text("bad")])
    agent = Agent(name="r", model=provider, output_type=Out, output_repair=False)
    with pytest.raises(OutputValidationError):
        await Runner.run(agent, "go")


def test_default_output_repair_attempt_cap() -> None:
    """Direct unit test for the default strategy's cap."""
    s = DefaultOutputRepair(max_attempts=1)
    exc = OutputValidationError("nope")
    assert s.build_prompt(exc, 1) is not None
    assert s.build_prompt(exc, 2) is None


# ---------- agent.tool_result_renderer ----------


@tool
async def get_data() -> dict:
    """Return a dict; we want the agent-level renderer to format it."""
    return {"x": 1}


@pytest.mark.asyncio
async def test_agent_level_tool_result_renderer_applied() -> None:
    """When a tool has no renderer of its own, the agent default is used."""
    seen: list[object] = []

    def renderer(result, ctx) -> str:
        seen.append(result)
        return f"AGENT:{result}"

    provider = ScriptedProvider([call("get_data", {}, call_id="c1"), text("done")])
    agent = Agent(
        name="r",
        model=provider,
        tools=[get_data],
        tool_result_renderer=renderer,
    )
    result = await Runner.run(agent, "go")
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.content == "AGENT:{'x': 1}"
    assert seen == [{"x": 1}]
