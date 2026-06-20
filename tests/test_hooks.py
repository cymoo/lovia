"""Tests for the subscriber-style AgentHooks."""

from __future__ import annotations

import pytest

from lovia import Agent, AgentHooks, RunContext, Runner, events, tool

from .scripted_provider import ScriptedProvider, call, text


@tool
async def echo(value: str) -> str:
    return value


@pytest.mark.asyncio
async def test_on_decorator_routes_specific_event_type() -> None:
    hooks = AgentHooks()
    captured: list[str] = []

    @hooks.on(events.ToolCallStarted)
    async def _(ev: events.ToolCallStarted, ctx: RunContext) -> None:
        captured.append(f"start:{ev.call.name}")

    @hooks.on(events.ToolCallCompleted)
    def sync_handler(ev: events.ToolCallCompleted, ctx: RunContext) -> None:
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
    def watch(ev: events.Event, ctx: RunContext) -> None:
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
    def watch(ev: events.Event, ctx: RunContext) -> None:
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
    def first(ev: events.RunCompleted, ctx: RunContext) -> None:
        order.append(1)

    @hooks.on(events.RunCompleted)
    def second(ev: events.RunCompleted, ctx: RunContext) -> None:
        order.append(2)

    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider, hooks=hooks)
    await Runner.run(agent, "go")

    assert order == [1, 2]


@pytest.mark.asyncio
async def test_handler_exception_is_logged_and_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hooks = AgentHooks()
    seen: list[str] = []

    @hooks.on(events.RunCompleted)
    def boom(ev: events.RunCompleted, ctx: RunContext) -> None:
        raise RuntimeError("observer crashed")

    @hooks.on(events.RunCompleted)
    def later(ev: events.RunCompleted, ctx: RunContext) -> None:
        seen.append("later")

    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider, hooks=hooks)
    with caplog.at_level("ERROR", logger="lovia.hooks"):
        result = await Runner.run(agent, "go")

    # The run completes, subsequent handlers still fire, and the failure
    # is logged with its traceback.
    assert result.output == "hi"
    assert seen == ["later"]
    assert any(r.exc_info for r in caplog.records)


@pytest.mark.asyncio
async def test_handler_receives_run_context() -> None:
    hooks = AgentHooks()
    seen: list[tuple[str, str | None]] = []

    @hooks.on(events.RunCompleted)
    async def _(ev: events.RunCompleted, ctx: RunContext) -> None:
        # Every handler is handed the live context as its second argument.
        seen.append((ctx.agent.name, ctx.session_id))

    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="ctxagent", model=provider, hooks=hooks)
    await Runner.run(agent, "go", session_id="s1")

    # The handler saw the active agent and the run's session key.
    assert seen == [("ctxagent", "s1")]


@pytest.mark.asyncio
async def test_on_any_handler_receives_ctx() -> None:
    hooks = AgentHooks()
    agents: set[str] = set()

    @hooks.on_any
    def _(ev: events.Event, ctx: RunContext) -> None:
        agents.add(ctx.agent.name)

    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="anyagent", model=provider, hooks=hooks)
    await Runner.run(agent, "go")

    assert agents == {"anyagent"}
