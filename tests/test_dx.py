"""Tests for the small DX wrappers: Agent.run/.run_sync/.stream, Runner.run_sync,
@tool(strict=True), Annotated parameter metadata, and friendly error hints.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import BaseModel, Field

from lovia import (
    Agent,
    CheckpointOptions,
    RunBudget,
    RunContext,
    Runner,
    tool,
)
from lovia.exceptions import (
    LoviaError,
    OutputValidationError,
    UserError,
)
from lovia.stores import InMemoryCheckpointer

from .scripted_provider import ScriptedProvider, call, text


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


def test_every_public_export_resolves() -> None:
    """Each name in ``lovia.__all__`` must exist (guards ``import *``)."""
    import lovia

    missing = [name for name in lovia.__all__ if not hasattr(lovia, name)]
    assert missing == []


def test_loviaerror_hint_appears_in_str() -> None:
    exc = LoviaError("something broke", hint="try again with --verbose")
    s = str(exc)
    assert "something broke" in s
    assert "hint:" in s
    assert "try again with --verbose" in s


class _Out(BaseModel):
    n: int


def _probe_agent(captured: dict, script, **agent_kwargs) -> Agent:
    """An agent whose first turn calls a ``probe`` tool that snapshots the ctx."""

    @tool
    def probe(ctx: RunContext) -> str:
        captured["deps"] = ctx.deps
        captured["context"] = ctx.context
        captured["run_id"] = ctx.run_id
        captured["turn"] = ctx.turn
        captured["budget"] = ctx.budget
        captured["system_prompt"] = ctx.system_prompt
        return "probed"

    return Agent(model=ScriptedProvider(script), tools=[probe], **agent_kwargs)


@pytest.mark.asyncio
async def test_run_context_exposes_deps_turn_and_system_prompt() -> None:
    captured: dict = {}
    agent = _probe_agent(
        captured,
        [call("probe", {}), text("done")],
        name="a",
        instructions="You are a careful calculator.",
    )
    deps = {"db": object()}
    result = await Runner.run(agent, "go", context=deps)

    assert result.output == "done"
    # deps is the friendly alias of context — same object.
    assert captured["deps"] is deps
    assert captured["context"] is deps
    # turn is 1-based and set before the tool runs on the first turn.
    assert captured["turn"] == 1
    # system_prompt is the rendered leading system text.
    assert "careful calculator" in captured["system_prompt"]
    # No checkpoint / budget configured.
    assert captured["run_id"] is None
    assert captured["budget"] is None


@pytest.mark.asyncio
async def test_run_context_run_id_from_checkpoint() -> None:
    captured: dict = {}
    agent = _probe_agent(captured, [call("probe", {}), text("ok")], name="a")
    cp = InMemoryCheckpointer()
    await Runner.run(agent, "go", checkpoint=CheckpointOptions(cp, "run-42"))
    assert captured["run_id"] == "run-42"


@pytest.mark.asyncio
async def test_run_context_exposes_budget_instance() -> None:
    captured: dict = {}
    agent = _probe_agent(captured, [call("probe", {}), text("ok")], name="a")
    budget = RunBudget(max_tool_calls=10)
    await Runner.run(agent, "go", budget=budget)
    assert captured["budget"] is budget


def test_run_result_repr_is_compact() -> None:
    provider = ScriptedProvider([text("a short answer")])
    agent = Agent(name="a", model=provider)
    result = Runner.run_sync(agent, "hi")
    r = repr(result)
    assert "RunResult(" in r
    assert "turns=" in r and "tokens=" in r
    # The noisy ``entries`` list must not be dumped into the repr.
    assert "entries=" not in r


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
