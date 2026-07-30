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


def test_create_app_auto_wires_and_manual_wire_is_idempotent() -> None:
    plugin = Subagents()
    app = _app(Agent(name="bot", model=None, plugins=[plugin]))
    # create_app adapted the plugin: supervised children + web delivery.
    assert plugin.deliver is not None
    assert plugin.run_child is not None
    assert wire_subagents(app) == 0  # already wired — left untouched

    # Opting out keeps core (bounded, in-process) semantics.
    plugin2 = Subagents()
    app2 = create_app(
        Agent(name="bot", model=None, plugins=[plugin2]),
        store=ChatStore.in_memory(),
        generate_titles=False,
        wire_subagents=False,
    )
    assert plugin2.deliver is None and plugin2.run_child is None
    assert wire_subagents(app2) == 1  # the manual helper still works


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
    """The whole supervised detached path: spawn -> child runs in its own
    task session -> parent run ends -> child finishes -> report starts a
    clientless run back in the parent session."""
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
    deps = app.state.deps  # auto-wired by create_app

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

    # The spawn became a task session: parent_id set, [t1]-prefixed title,
    # its own supervised run record.
    task_rows = [r for r in await deps.store.list() if r.parent_id == "s3"]
    assert len(task_rows) == 1
    task = task_rows[0]
    assert task.title is not None and task.title.startswith("[t1]")
    assert task.agent == "bot"  # follow-ups in the task chat use a served key
    child_run = await deps.store.latest_run_for(f"subagent:{task.id}")
    assert child_run is not None and child_run.session_id == task.id

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
    # The child's own transcript persisted in its task session too.
    child_entries = await deps.session.load(task.id)
    assert any(isinstance(e, InputEntry) and e.content == "dig" for e in child_entries)


async def test_cancel_subagent_cancels_supervised_child() -> None:
    gate = asyncio.Event()  # never set: the child stays parked
    child = Agent(name="slow", model=GatedProvider(ScriptedProvider([text("x")]), gate))
    provider = ScriptedProvider(
        [
            call("spawn_subagent", {"prompt": "dig"}),
            call("cancel_subagent", {"id": "t1"}),
            text("bye"),
        ]
    )
    agent = Agent(name="bot", model=provider, plugins=[Subagents(child)])
    app = _app(agent)
    deps = app.state.deps
    await deps.store.upsert("s4", agent="bot", title="chat")
    await deps.supervisor.start(
        session_id="s4",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    await _poll(lambda: _none(deps.supervisor.get("s4")))
    (task,) = [r for r in await deps.store.list() if r.parent_id == "s4"]

    async def _cancelled() -> bool:
        run = await deps.store.latest_run_for(f"subagent:{task.id}")
        return run is not None and run.status == "cancelled"

    await _poll(_cancelled)  # the token watcher routed through supervisor.cancel
    # A cancelled child delivers no report: no clientless delivery run starts.
    await asyncio.sleep(0.1)
    assert await deps.store.latest_run_for("subagent:t1") is None


async def test_failed_child_delivers_failure_report() -> None:
    child = Agent(name="doomed", model=ScriptedProvider([call("missing_tool", {})]))
    provider = ScriptedProvider(
        [
            call("spawn_subagent", {"prompt": "fail"}),
            text("bye"),
            text("noted"),  # the delivery run's reaction to the failure report
        ]
    )
    agent = Agent(name="bot", model=provider, plugins=[Subagents(child, max_turns=1)])
    app = _app(agent)
    deps = app.state.deps
    await deps.store.upsert("s5", agent="bot", title="chat")
    await deps.supervisor.start(
        session_id="s5",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )

    async def _delivered() -> bool:
        run = await deps.store.latest_run_for("subagent:t1")
        return run is not None and run.status == "completed"

    await _poll(_delivered)
    entries = await deps.session.load("s5")
    texts = [
        e.content
        for e in entries
        if isinstance(e, InputEntry) and isinstance(e.content, str)
    ]
    assert any("[subagent t1: failed]" in t for t in texts)


def test_sessions_api_exposes_parent_id() -> None:
    """Regression: the list endpoint's projection must carry parent_id — the
    sidebar's Tasks grouping reads it off GET /api/sessions, not the store."""
    from fastapi.testclient import TestClient

    agent = Agent(name="bot", model=ScriptedProvider([]))
    app = _app(agent)
    deps = app.state.deps

    async def seed() -> None:
        await deps.store.upsert("chat", agent="bot", title="chat")
        await deps.store.upsert("task", agent="bot", title="[t1] x", parent_id="chat")

    asyncio.run(seed())
    rows = {r["id"]: r for r in TestClient(app).get("/api/sessions").json()}
    assert rows["chat"]["parent_id"] is None
    assert rows["task"]["parent_id"] == "chat"


async def test_store_parent_id_roundtrip_and_legacy_migration(tmp_path) -> None:
    import sqlite3

    # A pre-0.9.17 database: chat_sessions without the parent_id column.
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE chat_sessions ("
            "id TEXT PRIMARY KEY, title TEXT, agent TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "active_run_id TEXT, pinned INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO chat_sessions VALUES ('old', 'Old chat', 'bot', 1, 1, NULL, 0)"
        )
    store = ChatStore.sqlite(db)
    old = await store.get("old")
    assert old is not None and old.parent_id is None  # migration added the column
    await store.upsert("kid", agent="bot", title="task", parent_id="old")
    kid = await store.get("kid")
    assert kid is not None and kid.parent_id == "old"
    listed = {r.id: r.parent_id for r in await store.list()}
    assert listed == {"old": None, "kid": "old"}
