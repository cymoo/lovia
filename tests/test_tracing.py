"""Tests for the tracer Protocol and bundled implementations."""

from __future__ import annotations

import logging

import pytest

from lovia import Agent, InMemoryTracer, Runner, tool
from lovia.tracing import ConsoleTracer, NoopTracer

from .scripted_provider import ScriptedProvider, call, text


@tool
async def add(a: int, b: int) -> int:
    return a + b


@pytest.mark.asyncio
async def test_in_memory_tracer_records_spans() -> None:
    tracer = InMemoryTracer()
    provider = ScriptedProvider(
        [
            call("add", {"a": 1, "b": 2}, call_id="c1"),
            text("3"),
        ]
    )
    agent = Agent(name="t", model=provider, tools=[add], tracer=tracer)

    result = await Runner.run(agent, "go")
    assert result.output == "3"

    names = [s.name for s in tracer.spans]
    # We expect exactly one run, two model_calls (one per turn), one tool call.
    assert names.count("run") == 1
    assert names.count("model_call") == 2
    assert names.count("tool") == 1

    run_span = next(s for s in tracer.spans if s.name == "run")
    assert run_span.attrs["agent"] == "t"
    assert run_span.attrs["turns"] == 2

    tool_span = next(s for s in tracer.spans if s.name == "tool")
    assert tool_span.attrs["name"] == "add"
    assert tool_span.exception is None


@pytest.mark.asyncio
async def test_console_tracer_emits_log_lines(caplog: pytest.LogCaptureFixture) -> None:
    tracer = ConsoleTracer(logger=logging.getLogger("lovia.trace.test"))
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider, tracer=tracer)

    with caplog.at_level(logging.INFO, logger="lovia.trace.test"):
        await Runner.run(agent, "go")

    # At least the run + one model_call line.
    messages = [r.message for r in caplog.records]
    assert any("run" in m for m in messages)
    assert any("model_call" in m for m in messages)


@pytest.mark.asyncio
async def test_noop_tracer_runs_cleanly() -> None:
    # The runner falls back to NoopTracer when ``agent.tracer`` is None;
    # NoopTracer should also be explicitly assignable.
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="t", model=provider, tracer=NoopTracer())
    result = await Runner.run(agent, "go")
    assert result.output == "ok"


@pytest.mark.asyncio
async def test_tool_span_records_exception() -> None:
    @tool
    async def boom() -> str:
        raise RuntimeError("kaboom")

    tracer = InMemoryTracer()
    provider = ScriptedProvider(
        [
            call("boom", {}, call_id="c1"),
            text("done"),
        ]
    )
    agent = Agent(name="t", model=provider, tools=[boom], tracer=tracer)
    await Runner.run(agent, "go")

    tool_span = next(s for s in tracer.spans if s.name == "tool")
    assert tool_span.exception is not None
    assert "kaboom" in str(tool_span.exception)
