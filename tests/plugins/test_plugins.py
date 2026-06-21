"""Tests for the general plugin mechanism: view injectors, instructions, hooks."""

from __future__ import annotations

import pytest

from lovia import Agent, AgentHooks, Runner
from lovia.exceptions import UserError
from lovia.plugins import Plugin, PluginInstance
from lovia.run_context import RunContext
from lovia.tools import tool
from lovia.transcript import InputEntry

from ..scripted_provider import ScriptedProvider, call, text


def _reminder_injector(text_value: str):
    def inject(ctx: RunContext):
        return [InputEntry(role="user", content=text_value)]

    return inject


def _plugin(
    *,
    name: str = "p",
    tools=(),
    injectors=(),
    instructions: str | None = None,
    hooks: AgentHooks | None = None,
) -> Plugin:
    inst = PluginInstance(
        tools=list(tools),
        view_injectors=list(injectors),
        instructions=instructions,
        hooks=hooks,
    )

    class _P:
        async def setup(self) -> PluginInstance:
            return inst

    p = _P()
    p.name = name  # type: ignore[attr-defined]
    return p  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_view_injector_reaches_model_but_is_not_persisted() -> None:
    # Two turns (tool call, then text answer) so we can assert the reminder is
    # re-injected on every model call.
    provider = ScriptedProvider([call("noop", {}), text("done")])

    @tool(name="noop")
    async def noop() -> str:
        return "ok"

    marker = "<system-reminder>INJECTED</system-reminder>"
    agent = Agent(
        name="t",
        model=provider,
        tools=[noop],
        plugins=[_plugin(injectors=[_reminder_injector(marker)])],
    )
    result = await Runner.run(agent, "go")

    # The injected reminder is in EVERY model view...
    assert all(
        any(m.role == "user" and marker in (m.content or "") for m in turn)
        for turn in provider.calls
    )
    # ...but never written to the persisted transcript.
    assert not any(
        isinstance(e, InputEntry) and marker in (e.content or "")
        for e in result.entries
    )


@pytest.mark.asyncio
async def test_multiple_injectors_append_in_order() -> None:
    provider = ScriptedProvider([text("hi")])
    agent = Agent(
        name="t",
        model=provider,
        plugins=[
            _plugin(name="a", injectors=[_reminder_injector("FIRST-MARK")]),
            _plugin(name="b", injectors=[_reminder_injector("SECOND-MARK")]),
        ],
    )
    await Runner.run(agent, "go")
    joined = "\n".join(m.content or "" for m in provider.calls[0])
    assert "FIRST-MARK" in joined and "SECOND-MARK" in joined
    assert joined.index("FIRST-MARK") < joined.index("SECOND-MARK")


@pytest.mark.asyncio
async def test_duplicate_plugin_name_is_rejected() -> None:
    # A plugin's name is its identity: two plugins sharing a name on one agent
    # is a config error, surfaced as a UserError before the run does any work.
    provider = ScriptedProvider([text("hi")])
    agent = Agent(
        name="t",
        model=provider,
        plugins=[_plugin(name="dup"), _plugin(name="dup")],
    )
    with pytest.raises(UserError, match="Duplicate plugin name 'dup'"):
        await Runner.run(agent, "go")


@pytest.mark.asyncio
async def test_async_injector_is_awaited() -> None:
    provider = ScriptedProvider([text("hi")])

    async def inject(ctx: RunContext):
        return [InputEntry(role="user", content="<system-reminder>ASYNC</system-reminder>")]

    agent = Agent(name="t", model=provider, plugins=[_plugin(injectors=[inject])])
    await Runner.run(agent, "go")
    assert any(
        m.role == "user" and "ASYNC" in (m.content or "")
        for turn in provider.calls
        for m in turn
    )


@pytest.mark.asyncio
async def test_failing_injector_is_skipped_fail_open() -> None:
    provider = ScriptedProvider([text("hi")])

    def boom(ctx: RunContext):
        raise RuntimeError("injector blew up")

    agent = Agent(name="t", model=provider, plugins=[_plugin(injectors=[boom])])
    # Run completes despite the broken injector.
    result = await Runner.run(agent, "go")
    assert result.output == "hi"


@pytest.mark.asyncio
async def test_plugin_contributes_tool_and_instructions() -> None:
    provider = ScriptedProvider([call("ping", {}), text("done")])

    @tool(name="ping")
    async def ping() -> str:
        return "pong"

    agent = Agent(
        name="t",
        model=provider,
        instructions="base",
        plugins=[_plugin(tools=[ping], instructions="PLUGIN-GUIDANCE")],
    )
    result = await Runner.run(agent, "go")
    # Tool was callable.
    assert any(e.type == "tool_result" for e in result.entries)
    # Instructions landed in the system prompt the model saw.
    system = provider.calls[0][0]
    assert system.role == "system"
    assert "PLUGIN-GUIDANCE" in (system.content or "")


@pytest.mark.asyncio
async def test_plugin_hooks_receive_events() -> None:
    provider = ScriptedProvider([text("hi")])
    seen: list[str] = []
    hooks = AgentHooks()

    @hooks.on_any
    def record(ev, ctx) -> None:
        seen.append(type(ev).__name__)

    agent = Agent(name="t", model=provider, plugins=[_plugin(hooks=hooks)])
    await Runner.run(agent, "go")
    assert "RunStarted" in seen
    assert "RunCompleted" in seen


@pytest.mark.asyncio
async def test_setup_called_per_run_gives_fresh_state() -> None:
    # A stateful plugin: setup builds a fresh list each run; concurrent runs
    # must not share it.
    class CountingPlugin:
        name = "counter"

        def __init__(self) -> None:
            self.setups = 0

        async def setup(self) -> PluginInstance:
            self.setups += 1
            return PluginInstance()

    plugin = CountingPlugin()
    agent = Agent(
        name="t", model=ScriptedProvider([text("a"), text("b")]), plugins=[plugin]
    )
    await Runner.run(agent, "one")
    await Runner.run(
        agent.clone(model=ScriptedProvider([text("b")])), "two"
    )
    assert plugin.setups == 2
