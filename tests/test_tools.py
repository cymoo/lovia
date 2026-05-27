"""Tests for tool middleware (``before`` / ``after`` hooks)."""

from __future__ import annotations

from typing import Any

import pytest

from lovia import Agent, Runner, tool

from .scripted_provider import ScriptedProvider, call, text


@pytest.mark.asyncio
async def test_before_can_mutate_args() -> None:
    seen: dict[str, Any] = {}

    async def before(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"city": args["city"].lower()}

    @tool(before=before)
    async def weather(city: str) -> str:
        seen["city"] = city
        return f"It is sunny in {city}"

    provider = ScriptedProvider([call("weather", {"city": "SHANGHAI"}), text("done")])
    agent = Agent(name="a", model=provider, tools=[weather])
    await Runner.run(agent, "hi")
    assert seen["city"] == "shanghai"


@pytest.mark.asyncio
async def test_after_can_rewrite_result() -> None:
    async def after(result: Any, ctx: Any) -> str:
        return f"[redacted:{result.split()[-1]}]"

    @tool(after=after)
    async def weather(city: str) -> str:
        return f"It is sunny in {city}"

    provider = ScriptedProvider([call("weather", {"city": "tokyo"}), text("ok")])
    agent = Agent(name="a", model=provider, tools=[weather])
    result = await Runner.run(agent, "hi")
    last_tool = next(m for m in reversed(result.messages) if m.role == "tool")
    assert last_tool.content == "[redacted:tokyo]"


@pytest.mark.asyncio
async def test_sync_middleware_supported() -> None:
    def before(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"n": args["n"] + 1}

    def after(result: Any, ctx: Any) -> str:
        return f"=> {result}"

    @tool(before=before, after=after)
    async def inc(n: int) -> int:
        return n * 10

    provider = ScriptedProvider([call("inc", {"n": 4}), text("done")])
    agent = Agent(name="a", model=provider, tools=[inc])
    result = await Runner.run(agent, "hi")
    last_tool = next(m for m in reversed(result.messages) if m.role == "tool")
    assert last_tool.content == "=> 50"


@pytest.mark.asyncio
async def test_before_exception_propagates_as_tool_error() -> None:
    async def before(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise ValueError("bad args")

    ran = False

    @tool(before=before)
    async def t() -> str:
        nonlocal ran
        ran = True
        return "ok"

    provider = ScriptedProvider([call("t", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[t])
    result = await Runner.run(agent, "go")
    assert ran is False
    last_tool = next(m for m in reversed(result.messages) if m.role == "tool")
    assert "bad args" in last_tool.content
