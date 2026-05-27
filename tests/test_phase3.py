"""Phase-3 tests: tool middleware + guardrails."""

from __future__ import annotations

from typing import Any

import pytest

from lovia import Agent, GuardrailTripped, Runner, tool

from .scripted_provider import ScriptedProvider, call, text


@pytest.mark.asyncio
async def test_tool_before_can_mutate_args() -> None:
    seen: dict[str, Any] = {}

    async def before(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # Normalize the city argument before the tool runs.
        return {"city": args["city"].lower()}

    async def after(result: Any, ctx: Any) -> str:
        seen["result"] = result
        return f"[redacted:{result.split()[-1]}]"

    @tool(before=before, after=after)
    async def weather(city: str) -> str:
        seen["city"] = city
        return f"It is sunny in {city}"

    provider = ScriptedProvider(
        [
            call("weather", {"city": "SHANGHAI"}),
            text("done"),
        ]
    )
    agent = Agent(name="a", model=provider, tools=[weather])
    result = await Runner.run(agent, "hi")
    assert seen["city"] == "shanghai"
    assert seen["result"] == "It is sunny in shanghai"
    # The tool-result message returned to the model is the after-redacted one.
    last_tool = next(m for m in reversed(result.messages) if m.role == "tool")
    assert last_tool.content == "[redacted:shanghai]"


@pytest.mark.asyncio
async def test_input_guardrail_blocks_run() -> None:
    async def block_banned(messages: list[Any], ctx: Any) -> str | None:
        joined = " ".join(getattr(m, "text", "") or "" for m in messages)
        if "banned" in joined:
            return "input contains banned word"
        return None

    provider = ScriptedProvider([text("never runs")])
    agent = Agent(name="a", model=provider, input_guardrails=[block_banned])
    with pytest.raises(GuardrailTripped, match="banned word"):
        await Runner.run(agent, "this is banned input")
    # No provider call should have been made.
    assert provider.calls == []


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
async def test_output_guardrail_passes() -> None:
    async def require_citation(output: Any, ctx: Any) -> str | None:
        return None if "[src]" in (output or "") else "missing citation"

    provider = ScriptedProvider([text("answer [src]")])
    agent = Agent(name="a", model=provider, output_guardrails=[require_citation])
    result = await Runner.run(agent, "hi")
    assert result.output == "answer [src]"


@pytest.mark.asyncio
async def test_sync_guardrail_supported() -> None:
    def block_all(messages: list[Any], ctx: Any) -> bool:
        return True

    provider = ScriptedProvider([text("never")])
    agent = Agent(name="a", model=provider, input_guardrails=[block_all])
    with pytest.raises(GuardrailTripped):
        await Runner.run(agent, "anything")
