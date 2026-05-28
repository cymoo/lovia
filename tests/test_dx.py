"""Tests for the small DX wrappers: Agent.run/.run_sync/.stream, Runner.run_sync,
@tool(strict=True), Annotated parameter metadata, and friendly error hints.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import BaseModel, Field

from lovia import Agent, Runner, tool
from lovia.exceptions import (
    LoviaError,
    OutputValidationError,
    UserError,
)

from .scripted_provider import ScriptedProvider, text


@pytest.mark.asyncio
async def test_agent_run_instance_method() -> None:
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="a", model=provider)
    result = await agent.run("hello")
    assert result.output == "hi"


@pytest.mark.asyncio
async def test_agent_stream_instance_method() -> None:
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="a", model=provider)
    handle = agent.stream("hello")
    events = [e async for e in handle]
    assert any(type(e).__name__ == "RunCompleted" for e in events)


def test_runner_run_sync_smoke() -> None:
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="a", model=provider)
    result = Runner.run_sync(agent, "hi")
    assert result.output == "ok"


def test_agent_run_sync_smoke() -> None:
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="a", model=provider)
    assert agent.run_sync("hi").output == "ok"


@pytest.mark.asyncio
async def test_run_sync_rejects_running_loop() -> None:
    provider = ScriptedProvider([text("x")])
    agent = Agent(name="a", model=provider)
    with pytest.raises(UserError) as exc_info:
        Runner.run_sync(agent, "hi")
    assert "running event loop" in str(exc_info.value)
    assert "await" in str(exc_info.value)


def test_tool_strict_makes_all_args_required() -> None:
    @tool(strict=True)
    def f(a: int, b: str = "x") -> str:
        return f"{a}:{b}"

    assert f.parameters["additionalProperties"] is False
    assert set(f.parameters["required"]) == {"a", "b"}


def test_tool_annotated_string_becomes_description() -> None:
    @tool
    def f(query: Annotated[str, "the search query"]) -> str:
        return query

    props = f.parameters["properties"]
    assert props["query"]["description"] == "the search query"


def test_tool_annotated_field_metadata_carried() -> None:
    @tool
    def f(n: Annotated[int, Field(ge=0, le=10, description="a number")]) -> int:
        return n

    schema = f.parameters["properties"]["n"]
    assert schema["description"] == "a number"
    assert schema["minimum"] == 0
    assert schema["maximum"] == 10


def test_loviaerror_hint_appears_in_str() -> None:
    exc = LoviaError("something broke", hint="try again with --verbose")
    s = str(exc)
    assert "something broke" in s
    assert "hint:" in s
    assert "try again with --verbose" in s


class _Out(BaseModel):
    n: int


@pytest.mark.asyncio
async def test_output_validation_error_carries_raw_and_hint() -> None:
    """Failed parse should attach raw text and a helpful hint."""
    provider = ScriptedProvider([text("not json"), text("not json again")])
    agent = Agent(name="a", model=provider, output_type=_Out, output_repair=False)
    with pytest.raises(OutputValidationError) as exc_info:
        await Runner.run(agent, "give me a number")
    err = exc_info.value
    assert err.hint and "output_repair" in err.hint
    assert err.raw and "not json" in err.raw
    assert err.output_type_name == "_Out"
