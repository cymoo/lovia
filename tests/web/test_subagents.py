"""Tests for web subagent delivery: wire_subagents + inject-or-start."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

pytest.importorskip("fastapi")

from lovia import Agent, Subagents  # noqa: E402
from lovia.plugins.subagents import SubagentReport  # noqa: E402
from lovia.transcript import InputEntry, ModelDelta, TranscriptEntry  # noqa: E402
from lovia.web import create_app, subagent_deliver, wire_subagents  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402


class GatedProvider:
    """Blocks every model call until ``gate`` is set, then delegates."""

    name = "gated"
    model = None
    supports_json_schema = False

    def __init__(self, inner: ScriptedProvider, gate: asyncio.Event) -> None:
        self.inner = inner
        self.gate = gate

    async def stream(
        self, entries: list[TranscriptEntry], **kwargs: Any
    ) -> AsyncIterator[ModelDelta]:
        await self.gate.wait()
        async for delta in self.inner.stream(entries, **kwargs):
            yield delta


def _report(
    session_id: str | None, *, text: str = "[subagent t1: done] news"
) -> SubagentReport:
    return SubagentReport(
        id="t1",
        agent="researcher",
        prompt="p",
        session_id=session_id,
        result=None,
        error=None,
        text=text,
    )


def _app(agent_or_agents):
    return create_app(
        agent_or_agents, store=ChatStore.in_memory(), generate_titles=False
    )


async def _poll(predicate, *, timeout: float = 5.0) -> None:
    async def _loop() -> None:
        while not await predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_loop(), timeout=timeout)


async def _none(value: Any) -> bool:
    return value is None


def test_wire_subagents_sets_deliver_once() -> None:
    plugin = Subagents()
    app = _app(Agent(name="bot", model=None, plugins=[plugin]))
    assert wire_subagents(app) == 1
    assert plugin.deliver is not None
    # Idempotent: already-wired (or custom) delivers are left untouched.
    assert wire_subagents(app) == 0


async def test_deliver_injects_into_live_run_and_autochain_consumes() -> None:
    gate = asyncio.Event()
    provider = ScriptedProvider([text("first"), text("reacted to the report")])
    agent = Agent(name="bot", model=GatedProvider(provider, gate))
    app = _app(agent)
    deps = app.state.deps
    await deps.store.upsert("s1", agent="bot", title="chat")
    await deps.supervisor.start(
        session_id="s1",
        agent=agent,
        input="hi",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    ctrl = deps.supervisor.get("s1")
    assert ctrl is not None

    await subagent_deliver(deps)(_report("s1"))
    assert bool(ctrl.mailbox)  # injected into the live run's mailbox

    # Let the parked model call finish: the leftover report auto-chains a
    # second leg that consumes it as a user turn.
    gate.set()
    await _poll(lambda: _none(deps.supervisor.get("s1")))
    entries = await deps.session.load("s1")
    texts = [
        e.content
        for e in entries
        if isinstance(e, InputEntry) and isinstance(e.content, str)
    ]
    assert any("[subagent t1: done]" in t for t in texts)


async def test_deliver_starts_clientless_run_when_idle() -> None:
    agent = Agent(name="bot", model=ScriptedProvider([text("got it")]))
    app = _app(agent)
    deps = app.state.deps
    await deps.store.upsert("s2", agent="bot", title="chat")

    await subagent_deliver(deps)(_report("s2"))
    await _poll(lambda: _none(deps.supervisor.get("s2")))

    entries = await deps.session.load("s2")
    texts = [
        e.content
        for e in entries
        if isinstance(e, InputEntry) and isinstance(e.content, str)
    ]
    assert any("[subagent t1: done]" in t for t in texts)
    run = await deps.store.latest_run_for("subagent:t1")
    assert run is not None
    assert run.status == "completed"


async def test_deliver_drops_when_session_is_gone() -> None:
    agent = Agent(name="bot", model=ScriptedProvider([]))
    app = _app(agent)
    deps = app.state.deps
    await subagent_deliver(deps)(_report("missing"))
    assert deps.supervisor.get("missing") is None
    await subagent_deliver(deps)(_report(None))  # sessionless: dropped quietly


async def test_wired_plugin_delivers_end_to_end() -> None:
    """The whole detached path under the web app: spawn -> parent run ends ->
    child finishes -> report starts a clientless run in the same session."""
    child_gate = asyncio.Event()
    child = Agent(
        name="researcher",
        model=GatedProvider(ScriptedProvider([text("late findings")]), child_gate),
    )
    plugin = Subagents(child)
    provider = ScriptedProvider(
        [
            call("spawn_subagent", {"prompt": "dig"}),
            text("bye"),  # parent finishes while the child is parked
            text("noted"),  # the delivery run's reaction
        ]
    )
    agent = Agent(name="bot", model=provider, plugins=[plugin])
    app = _app(agent)
    deps = app.state.deps
    assert wire_subagents(app) == 1

    await deps.store.upsert("s3", agent="bot", title="chat")
    await deps.supervisor.start(
        session_id="s3",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    await _poll(lambda: _none(deps.supervisor.get("s3")))  # parent run over

    child_gate.set()

    async def _delivered() -> bool:
        run = await deps.store.latest_run_for("subagent:t1")
        return run is not None and run.status == "completed"

    await _poll(_delivered)
    entries = await deps.session.load("s3")
    texts = [
        e.content
        for e in entries
        if isinstance(e, InputEntry) and isinstance(e.content, str)
    ]
    assert any("late findings" in t for t in texts)
