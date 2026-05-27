"""Tests for the subscriber-style AgentHooks."""

from __future__ import annotations

import pytest

from lovia import Agent, AgentHooks, Runner, events, tool

from .scripted_provider import ScriptedProvider, call, text


@tool
async def echo(value: str) -> str:
    return value


@pytest.mark.asyncio
async def test_on_decorator_routes_specific_event_type() -> None:
    hooks = AgentHooks()
    captured: list[str] = []

    @hooks.on(events.ToolCallStarted)
    async def _(ev: events.ToolCallStarted) -> None:
        captured.append(f"start:{ev.call.name}")

    @hooks.on(events.ToolCallCompleted)
    def sync_handler(ev: events.ToolCallCompleted) -> None:
        captured.append(f"end:{ev.call.name}")

    provider = ScriptedProvider(
        [
            call("echo", {"value": "hi"}, call_id="c1"),
            text("done"),
        ]
    )
    agent = Agent(name="t", model=provider, tools=[echo], hooks=hooks)
    await Runner.run(agent, "go")

    assert captured == ["start:echo", "end:echo"]


@pytest.mark.asyncio
async def test_on_tuple_of_types_fires_on_either() -> None:
    hooks = AgentHooks()
    seen: list[str] = []

    @hooks.on((events.RunStarted, events.RunCompleted))
    def watch(ev: events.Event) -> None:
        seen.append(type(ev).__name__)

    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="t", model=provider, hooks=hooks)
    await Runner.run(agent, "go")

    # RunStarted is currently not emitted by the runner; if added later, this
    # test still asserts that RunCompleted reaches the handler.
    assert "RunCompleted" in seen


@pytest.mark.asyncio
async def test_on_any_receives_every_event() -> None:
    hooks = AgentHooks()
    names: list[str] = []

    @hooks.on_any
    def watch(ev: events.Event) -> None:
        names.append(type(ev).__name__)

    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider, hooks=hooks)
    await Runner.run(agent, "go")

    # We should see at least Turn + MessageCompleted + RunCompleted.
    assert "RunCompleted" in names
    assert "MessageCompleted" in names


@pytest.mark.asyncio
async def test_multiple_handlers_same_event_type_run_in_order() -> None:
    hooks = AgentHooks()
    order: list[int] = []

    @hooks.on(events.RunCompleted)
    def first(ev: events.RunCompleted) -> None:
        order.append(1)

    @hooks.on(events.RunCompleted)
    def second(ev: events.RunCompleted) -> None:
        order.append(2)

    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider, hooks=hooks)
    await Runner.run(agent, "go")

    assert order == [1, 2]
