"""Tests for input / output guardrails."""

from __future__ import annotations

from typing import Any

import pytest

from lovia import Agent, GuardrailTripped, Runner

from .scripted_provider import ScriptedProvider, text


@pytest.mark.asyncio
async def test_input_guardrail_blocks_run_before_provider_call() -> None:
    async def block_banned(messages: list[Any], ctx: Any) -> str | None:
        joined = " ".join(getattr(m, "text", "") or "" for m in messages)
        return "banned word" if "banned" in joined else None

    provider = ScriptedProvider([text("never runs")])
    agent = Agent(name="a", model=provider, input_guardrails=[block_banned])
    with pytest.raises(GuardrailTripped, match="banned word"):
        await Runner.run(agent, "this is banned input")
    assert provider.calls == []


@pytest.mark.asyncio
async def test_input_guardrail_returning_none_lets_run_proceed() -> None:
    async def allow(messages: list[Any], ctx: Any) -> str | None:
        return None

    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="a", model=provider, input_guardrails=[allow])
    result = await Runner.run(agent, "hi")
    assert result.output == "ok"


@pytest.mark.asyncio
async def test_output_guardrail_runs_after_completion() -> None:
    async def require_citation(output: Any, ctx: Any) -> str | None:
        if isinstance(output, str) and "[src]" not in output:
            return "missing citation"
        return None

    provider = ScriptedProvider([text("answer without source")])
    agent = Agent(name="a", model=provider, output_guardrails=[require_citation])
    with pytest.raises(GuardrailTripped, match="missing citation"):
        await Runner.run(agent, "hi")


@pytest.mark.asyncio
async def test_output_guardrail_passing_yields_normal_result() -> None:
    async def require_citation(output: Any, ctx: Any) -> str | None:
        return None if "[src]" in (output or "") else "missing citation"

    provider = ScriptedProvider([text("answer [src]")])
    agent = Agent(name="a", model=provider, output_guardrails=[require_citation])
    result = await Runner.run(agent, "hi")
    assert result.output == "answer [src]"


@pytest.mark.asyncio
async def test_sync_guardrail_returning_bool_is_supported() -> None:
    def block_all(messages: list[Any], ctx: Any) -> bool:
        return True

    provider = ScriptedProvider([text("never")])
    agent = Agent(name="a", model=provider, input_guardrails=[block_all])
    with pytest.raises(GuardrailTripped):
        await Runner.run(agent, "anything")


@pytest.mark.asyncio
async def test_multiple_guardrails_run_in_declaration_order() -> None:
    order: list[str] = []

    async def first(messages: list[Any], ctx: Any) -> str | None:
        order.append("first")
        return None

    async def second(messages: list[Any], ctx: Any) -> str | None:
        order.append("second")
        return "stop here"

    async def third(messages: list[Any], ctx: Any) -> str | None:  # pragma: no cover
        order.append("third")
        return None

    provider = ScriptedProvider([text("never")])
    agent = Agent(name="a", model=provider, input_guardrails=[first, second, third])
    with pytest.raises(GuardrailTripped, match="stop here"):
        await Runner.run(agent, "hi")
    assert order == ["first", "second"]
