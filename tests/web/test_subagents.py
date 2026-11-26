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
    return create_app(agent_or_agents, store=ChatStore.in_memory(), titles=False)


async def _poll(predicate, *, timeout: float = 5.0) -> None:
    async def _loop() -> None:
        while not await predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_loop(), timeout=timeout)


async def _none(value: Any) -> bool:
    return value is None


async def _task_of(deps: Any, parent: str, *, live: bool = False) -> Any:
    """``parent``'s one task session once registered (and with ``live``, its
    run started): a detached spawn registers it in the background, possibly
    after the parent's run has ended."""

    async def ready() -> bool:
        kids = await deps.store.list_children(parent)
        return len(kids) == 1 and (
            not live or deps.supervisor.get(kids[0].id) is not None
        )

    await _poll(ready)
    (task,) = await deps.store.list_children(parent)
    return task


def test_create_app_auto_wires_and_manual_wire_is_idempotent() -> None:
    plugin = Subagents()
    app = _app(Agent(name="bot", model=None, plugins=[plugin]))
    # create_app adapted the plugin: supervised children + web delivery.
    assert plugin.deliver is not None
    assert plugin.run_child is not None
    assert wire_subagents(app.state.deps) == 0  # already wired — left untouched

    # Opting out keeps core (bounded, in-process) semantics.
    plugin2 = Subagents()
    create_app(
        Agent(name="bot", model=None, plugins=[plugin2]),
        store=ChatStore.in_memory(),
        titles=False,
        wire_subagents=False,
    )
    assert plugin2.deliver is None and plugin2.run_child is None


def test_wire_subagents_for_embedding_apps() -> None:
    # An app mounting build_api_router wires through the RouterDeps it built.
    from lovia.web import RouterDeps

    plugin = Subagents()
    deps = RouterDeps(
        agents={"bot": Agent(name="bot", model=None, plugins=[plugin])},
        store=ChatStore.in_memory(),
    )
    assert wire_subagents(deps) == 1
    assert plugin.deliver is not None and plugin.run_child is not None


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
    run = await deps.store.latest_run_for("subagent-report:t1")
    assert run is not None
    assert run.status == "completed"


async def test_deliver_drops_when_session_is_gone() -> None:
    agent = Agent(name="bot", model=ScriptedProvider([]))
    app = _app(agent)
    deps = app.state.deps
    await subagent_deliver(deps)(_report("missing"))
    assert deps.supervisor.get("missing") is None
    await subagent_deliver(deps)(_report(None))  # sessionless: dropped quietly


async def test_wired_plugin_delivers_end_to_end(caplog) -> None:
    """The whole supervised detached path: spawn -> child runs in its own
    task session -> parent run ends -> child finishes -> report starts a
    clientless run back in the parent session."""
    import logging

    caplog.set_level(logging.INFO, logger="lovia.web")
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
    emitted: list[tuple[str, dict]] = []
    orig_emit = deps.emit
    deps.emit = lambda name, data: (
        emitted.append((name, dict(data))),
        orig_emit(name, data),
    )[1]  # type: ignore[method-assign]

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
    task = await _task_of(deps, "s3")
    assert not [r for r in await deps.store.list() if r.parent_id]  # chats only
    assert task.title is not None and task.title.startswith("[t1]")
    assert task.agent == "bot"  # follow-ups in the task chat use a served key

    async def _child_recorded() -> bool:
        return await deps.store.latest_run_for(f"subagent:{task.id}") is not None

    # The child's own task writes its run record, after the session registers.
    await _poll(_child_recorded)
    child_run = await deps.store.latest_run_for(f"subagent:{task.id}")
    assert child_run is not None and child_run.session_id == task.id

    child_gate.set()

    async def _delivered() -> bool:
        run = await deps.store.latest_run_for("subagent-report:t1")
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
    # The sidebar learns about the task session immediately (scheduler parity).
    assert any(
        name == "session_created" and data["session_id"] == task.id
        for name, data in emitted
    )
    # The lifecycle is legible in the logs: spawn, end, delivery.
    assert f"task session {task.id} started" in caplog.text
    assert f"task session {task.id} ended: completed" in caplog.text
    assert "report delivered to idle session s3" in caplog.text


async def test_run_source_filter_tags_task_context_logs() -> None:
    import logging

    from lovia.log_config import CURRENT_AGENT
    from lovia.web.supervisor import RUN_SOURCE, run_source_log_filter

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    handler.addFilter(run_source_log_filter())
    lg = logging.getLogger("lovia.web._source_test")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    try:
        lg.info("outside")

        async def inner() -> None:
            RUN_SOURCE.set("subagent:abc")  # what RunController._run does
            lg.info("inside")
            CURRENT_AGENT.set("digger")  # what RunLoop.stream does
            lg.info("inside-run")

        async def agent_only() -> None:
            CURRENT_AGENT.set("followups")  # a parentless helper run
            lg.info("helper")

        async def hostile_name() -> None:
            # A free-form agent name must not split the one-line prefix.
            CURRENT_AGENT.set("bad\nname")
            lg.info("hostile")

        # create_task copies the context, so the tags stay task-local.
        await asyncio.create_task(inner())
        await asyncio.create_task(agent_only())
        await asyncio.create_task(hostile_name())
        lg.info("outside-after")
    finally:
        lg.removeHandler(handler)
    tags = {r.getMessage(): r.run_source for r in records}
    assert tags == {
        "outside": "",
        "inside": "[subagent:abc] ",
        "inside-run": "[subagent:abc/digger] ",
        "helper": "[followups] ",
        "hostile": "[bad name] ",
        "outside-after": "",
    }


async def test_cancel_subagent_cancels_supervised_child(caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="lovia.web")
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
    task = await _task_of(deps, "s4")

    async def _cancelled() -> bool:
        run = await deps.store.latest_run_for(f"subagent:{task.id}")
        return run is not None and run.status == "cancelled"

    await _poll(_cancelled)  # the token watcher routed through supervisor.cancel
    # A cancelled child delivers no report: no clientless delivery run starts.
    await asyncio.sleep(0.1)
    assert await deps.store.latest_run_for("subagent-report:t1") is None
    # The task's logs say who stopped it — not "user requested stop".
    assert "cancelled by the parent agent" in caplog.text
    assert "user requested stop" not in caplog.text


async def test_deleting_a_chat_stops_and_deletes_its_tasks(caplog) -> None:
    import logging

    import httpx

    caplog.set_level(logging.INFO, logger="lovia.web")
    gate = asyncio.Event()  # parks the child in its model call
    child = Agent(name="slow", model=GatedProvider(ScriptedProvider([text("x")]), gate))
    provider = ScriptedProvider([call("spawn_subagent", {"prompt": "dig"}), text("ok")])
    plugin = Subagents(child)
    agent = Agent(name="bot", model=provider, plugins=[plugin])
    app = _app(agent)
    deps = app.state.deps
    await deps.store.upsert("s6", agent="bot", title="chat")
    await deps.supervisor.start(
        session_id="s6",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    await _poll(lambda: _none(deps.supervisor.get("s6")))
    task = await _task_of(deps, "s6", live=True)  # outlives its parent's run

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        delete = asyncio.create_task(ac.delete("/api/sessions/s6"))
        await _poll(lambda: _none(deps.supervisor.get(task.id)))  # stopped
        # A stop is cooperative: the model call in flight completes first,
        # and the delete waits that out.
        gate.set()
        assert (await asyncio.wait_for(delete, 5)).status_code == 200
    assert await deps.store.get(task.id) is None
    assert await deps.session.load(task.id) == []
    assert await deps.store.list_runs() == []
    # A task stopped by the delete reports nothing to the deleted chat.
    await asyncio.gather(*plugin._detached, return_exceptions=True)
    assert list(deps.supervisor) == []
    assert (
        f"run stop requested: session={task.id} (the chat was deleted)" in caplog.text
    )


async def test_a_spawn_still_in_run_deps_does_not_outlive_a_deleted_chat() -> None:
    # The spawn's run_deps outlasts its parent run and the delete; once it
    # returns, the task must not register under the deleted chat.
    import httpx

    entered, release = asyncio.Event(), asyncio.Event()

    async def run_deps(trigger: Any) -> None:
        if trigger.source.startswith("subagent:"):
            entered.set()
            await release.wait()

    child = Agent(name="child", model=ScriptedProvider([text("done")]))
    plugin = Subagents(child)
    provider = ScriptedProvider([call("spawn_subagent", {"prompt": "dig"}), text("ok")])
    agent = Agent(name="bot", model=provider, plugins=[plugin])
    app = create_app(
        agent, store=ChatStore.in_memory(), titles=False, run_deps=run_deps
    )
    deps = app.state.deps
    await deps.store.upsert("s7", agent="bot", title="chat")
    ctrl = await deps.supervisor.start(
        session_id="s7",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    await asyncio.wait_for(entered.wait(), 5)
    assert ctrl.task is not None
    await asyncio.wait_for(ctrl.task, 5)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        assert (await ac.delete("/api/sessions/s7")).status_code == 200
    release.set()
    await asyncio.gather(*plugin._detached, return_exceptions=True)
    assert await deps.store.list_children("s7") == []
    assert list(deps.supervisor) == []


async def test_a_report_still_in_run_deps_does_not_restart_a_deleted_chat() -> None:
    import httpx

    entered, release = asyncio.Event(), asyncio.Event()

    async def run_deps(trigger: Any) -> None:
        if trigger.source.startswith("subagent-report:"):
            entered.set()
            await release.wait()

    child = Agent(name="child", model=ScriptedProvider([text("findings")]))
    plugin = Subagents(child)
    provider = ScriptedProvider(
        [call("spawn_subagent", {"prompt": "dig"}), text("ok"), text("noted")]
    )
    agent = Agent(name="bot", model=provider, plugins=[plugin])
    app = create_app(
        agent, store=ChatStore.in_memory(), titles=False, run_deps=run_deps
    )
    deps = app.state.deps
    await deps.store.upsert("s8", agent="bot", title="chat")
    await deps.supervisor.start(
        session_id="s8",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    await asyncio.wait_for(entered.wait(), 5)  # the task is done; its report waits
    await _poll(lambda: _none(deps.supervisor.get("s8")))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        assert (await ac.delete("/api/sessions/s8")).status_code == 200
    release.set()
    await asyncio.gather(*plugin._detached, return_exceptions=True)
    assert list(deps.supervisor) == []
    assert await deps.session.load("s8") == []
    assert await deps.store.list_runs() == []


async def test_a_stop_while_the_report_is_in_run_deps_still_holds_it() -> None:
    """The chat was idle when the report came; while its run_deps ran, the
    user started a run there and stopped it. The stop holds."""
    import time

    from lovia.web.store import RunRow

    entered, release = asyncio.Event(), asyncio.Event()

    async def run_deps(trigger: Any) -> None:
        if trigger.source.startswith("subagent-report:"):
            entered.set()
            await release.wait()

    child = Agent(name="child", model=ScriptedProvider([text("findings")]))
    plugin = Subagents(child)
    provider = ScriptedProvider([call("spawn_subagent", {"prompt": "dig"}), text("ok")])
    agent = Agent(name="bot", model=provider, plugins=[plugin])
    app = create_app(
        agent, store=ChatStore.in_memory(), titles=False, run_deps=run_deps
    )
    deps = app.state.deps
    await deps.store.upsert("s9", agent="bot", title="chat")
    await deps.supervisor.start(
        session_id="s9",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    await asyncio.wait_for(entered.wait(), 5)  # past the first stop check
    await _poll(lambda: _none(deps.supervisor.get("s9")))
    now = time.time() + 1  # the user's run, started and stopped meanwhile
    await deps.store.start_run(
        RunRow("r-stopped", "s9", "bot", "user", "cancelled", None, now, now, None)
    )
    release.set()
    await asyncio.gather(*plugin._detached, return_exceptions=True)
    assert await deps.store.latest_run_for("subagent-report:t1") is None
    report = (await deps.session.load("s9"))[-1]
    assert isinstance(report, InputEntry) and "findings" in str(report.content)


async def test_overlapping_deletes_keep_the_tree_closed_until_the_last_ends() -> None:
    deps = _app(Agent(name="bot", model=ScriptedProvider([]))).state.deps
    await deps.store.upsert("c", agent="bot")
    await deps.store.upsert("t", agent="bot", parent_id="c")

    async def alive(sid: str) -> bool:
        async with deps._chat_alive(sid) as ok:
            return ok

    assert await alive("c") and await alive("t")
    async with deps._deleting_tree("c") as tree:
        assert sorted(tree) == ["c", "t"]
        async with deps._deleting_tree("c"):
            pass  # an overlapping delete of the same chat ends (or fails) first
        assert not await alive("c") and not await alive("t")
    assert await alive("c") and await alive("t")
    assert not deps._deleting


async def test_a_report_after_a_stop_is_kept_without_starting_a_run() -> None:
    """The user stopped the chat; a task finishing later must not start the
    agent again. Its report is kept in the chat, announced to open pages, and
    the next message's run reads it."""
    from lovia import tool

    child_gate, parent_gate = asyncio.Event(), asyncio.Event()

    @tool
    async def wait() -> str:
        """Block until released."""
        await parent_gate.wait()
        return "released"

    child = Agent(
        name="researcher",
        model=GatedProvider(ScriptedProvider([text("late findings")]), child_gate),
    )
    provider = ScriptedProvider(
        [
            call("spawn_subagent", {"prompt": "dig"}),
            call("wait", {}),
            text("read it"),  # the next message's reply
        ]
    )
    agent = Agent(name="bot", model=provider, tools=[wait], plugins=[Subagents(child)])
    app = _app(agent)
    deps = app.state.deps
    emitted: list[tuple[str, dict]] = []
    orig_emit = deps.emit
    deps.emit = lambda name, data: (
        emitted.append((name, dict(data))),
        orig_emit(name, data),
    )[1]  # type: ignore[method-assign]
    await deps.store.upsert("s7", agent="bot", title="chat")
    await deps.supervisor.start(
        session_id="s7",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    await _task_of(deps, "s7", live=True)
    deps.supervisor.cancel("s7")  # the user's stop, mid-`wait`
    # The task finishes while the stopped run still winds down (its tool has
    # not returned): its record doesn't read "cancelled" yet.
    child_gate.set()

    async def _kept() -> bool:
        return any(n == "session_appended" for n, _ in emitted)

    await _poll(_kept)
    parent_gate.set()
    await deps.supervisor.drain("s7")
    assert await deps.store.latest_run_for("subagent-report:t1") is None
    entries = await deps.session.load("s7")
    report = entries[-1]  # after the turns the stop saved
    assert isinstance(report, InputEntry) and "late findings" in str(report.content)
    assert ("session_appended", {"session_id": "s7"}) in emitted

    # The next message's run reads it.
    await deps.supervisor.start(
        session_id="s7",
        agent=agent,
        input="what came back?",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    await _poll(lambda: _none(deps.supervisor.get("s7")))
    seen = [str(m.content) for m in provider.calls[-1]]
    assert any("late findings" in s for s in seen)


async def test_a_settled_stop_still_holds_reports() -> None:
    """Once the stopped run has wound down only its record says so. A later
    finished run ends the hold."""
    from lovia.web.store import RunRow
    from lovia.web.subagents import _stopped

    deps = _app(Agent(name="bot", model=ScriptedProvider([]))).state.deps
    await deps.store.upsert("s8", agent="bot", title="chat")

    async def record(rid: str, status: str, at: float) -> None:
        await deps.store.start_run(
            RunRow(rid, "s8", "bot", "user", status, None, at, at, None)
        )

    assert not await _stopped(deps, "s8")  # no runs yet
    await record("r1", "cancelled", 1.0)
    assert await _stopped(deps, "s8")
    await record("r2", "completed", 2.0)
    assert not await _stopped(deps, "s8")


def test_the_chat_recognises_what_the_core_renders() -> None:
    """The chat draws a report as a card by matching the text the core's
    ``_render`` writes (static/js/subagent-report.js). Both reports match
    that same pattern, so neither side can change its wording alone."""
    import re
    import time
    from pathlib import Path
    from types import SimpleNamespace

    from lovia.plugins.subagents import _Record, _render
    from lovia.reliability import CancelToken
    from lovia.steering import Mailbox

    js = Path(__file__).parents[2] / "lovia/web/static/js/subagent-report.js"
    literal = re.search(r"const REPORT = /(.+)/;", js.read_text()).group(1)  # type: ignore[union-attr]
    pattern = re.compile(literal)

    def record(**kw: Any) -> _Record:
        return _Record(
            id="t1",
            spawn_id="s1",
            agent_name="lovia.sub",
            prompt="p",
            token=CancelToken(),
            mailbox=Mailbox(),
            started=time.monotonic(),
            **kw,
        )

    result = SimpleNamespace(output="## Found\nit")  # all _render reads
    done = pattern.match(_render(record(result=result), max_chars=None))
    assert done is not None
    assert done.groups() == ("t1", "done", "lovia.sub", "0", "## Found\nit")
    failed = pattern.match(_render(record(error=RuntimeError("boom")), max_chars=None))
    assert failed is not None and failed.group(2) == "failed"
    assert failed.group(5) == "RuntimeError: boom"


async def test_each_spawns_card_leads_to_its_own_task() -> None:
    """Two turns of one run spawn under the same provider call id, both as
    ``t1``/``t2`` of that run — and a later run starts at ``t1`` again: the
    text a card holds (as static/js/tools.js reads it) still names one task."""
    import re
    from pathlib import Path

    from lovia.transcript import ToolResultEntry

    never = asyncio.Event()  # no report comes to start runs of its own
    child = Agent(
        name="re (search)er", model=GatedProvider(ScriptedProvider([]), never)
    )
    provider = ScriptedProvider(
        [
            call("spawn_subagent", {"prompt": "one"}, call_id="same"),
            call("spawn_subagent", {"prompt": "two"}, call_id="same"),
            text("bye"),
            call("spawn_subagent", {"prompt": "three"}, call_id="same"),
            text("bye"),
        ]
    )
    agent = Agent(name="bot", model=provider, plugins=[Subagents(child)])
    app = _app(agent)
    deps = app.state.deps
    await deps.store.upsert("s7", agent="bot", title="chat")

    async def go() -> None:
        await deps.supervisor.start(
            session_id="s7",
            agent=agent,
            input="go",
            is_new=False,
            title_message=None,
            autostart=True,
            source="user",
        )
        await _poll(lambda: _none(deps.supervisor.get("s7")))

    js = (Path(__file__).parents[2] / "lovia/web/static/js/tools.js").read_text()
    literal = re.search(r"const STARTED_TASK = /(.+)/;", js).group(1)  # type: ignore[union-attr]

    async def cards() -> list[str]:
        return [
            m.group(1)
            for e in await deps.session.load("s7")
            if isinstance(e, ToolResultEntry) and (m := re.match(literal, e.output))
        ]

    async def spawned(n: int) -> bool:
        return len(await cards()) == n and (
            len(await deps.store.list_children("s7")) == n
        )

    await go()
    await _poll(lambda: spawned(2))
    await go()
    await _poll(lambda: spawned(3))

    tasks = await deps.store.list_children("s7")
    titles = {m.origin_ref: m.title for m in tasks}
    assert [titles[spawn_id] for spawn_id in await cards()] == [
        "[t1] one",
        "[t2] two",
        "[t1] three",
    ]
    for m in tasks:
        deps.supervisor.cancel(m.id)
    await deps.supervisor.shutdown()


async def test_a_tasks_step_and_summary() -> None:
    import httpx

    from lovia import tool

    gate = asyncio.Event()
    entered = asyncio.Event()

    @tool
    async def dig(path: str, content: str, depth: int = 1) -> str:
        """Dig."""
        entered.set()
        await gate.wait()
        return "dug"

    child = Agent(
        name="digger",
        tools=[dig],
        model=ScriptedProvider(
            [
                call("missing_tool", {}),
                call("dig", {"path": "x" * 1000, "content": "secret", "depth": 2}),
                text("**found** it"),
            ]
        ),
    )
    provider = ScriptedProvider(
        [call("spawn_subagent", {"prompt": "dig in"}), text("bye"), text("noted")]
    )
    agent = Agent(name="bot", model=provider, plugins=[Subagents(child)])
    app = _app(agent)
    deps = app.state.deps
    await deps.store.upsert("s8", agent="bot", title="chat")
    await deps.supervisor.start(
        session_id="s8",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    await asyncio.wait_for(entered.wait(), 5)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        (row,) = (await c.get("/api/sessions", params={"parent": "s8"})).json()
        task = row["id"]
        assert row["spawn_id"] and row["last_run_seconds"] is None
        runs = {r["session_id"]: r for r in (await c.get("/api/runs")).json()}
        step = runs[task]["step"]
        # What a line is made of, short: never what the call writes.
        assert step == {"name": "dig", "args": {"path": "x" * 300}}

        live = (await c.get(f"/api/sessions/{task}/summary")).json()
        assert live["prompt"] == "dig in"
        assert live["step"] == step
        assert (live["tool_calls"], live["failed_calls"]) == (2, 1)
        assert live["report"] is None

        gate.set()
        await _poll(lambda: _none(deps.supervisor.get(task)))

        async def over() -> bool:
            rows = (await c.get("/api/sessions", params={"parent": "s8"})).json()
            return rows[0]["last_run_seconds"] is not None

        await _poll(over)
        done = (await c.get(f"/api/sessions/{task}/summary")).json()
        assert done == {
            "prompt": "dig in",
            "step": None,
            "tool_calls": 2,
            "failed_calls": 1,
            "report": "**found** it",
            "error": None,
        }
        missing = await c.get("/api/sessions/nope/summary")
        assert missing.status_code == 404


def test_a_summary_reports_only_a_run_that_completed() -> None:
    from lovia.web.api.serialization import session_summary
    from lovia.web.schemas import MessageOut
    from lovia.web.store import LatestRun

    view = [
        MessageOut(role="user", content="go"),
        MessageOut(role="assistant", content="I'll check the file"),
    ]

    def summary(status: str, live: bool = False) -> dict:
        last = LatestRun(status, 1.0, 5.0, "boom" if status == "failed" else None)
        return session_summary(view, live=live, step=None, last_run=last).model_dump()

    assert summary("completed")["report"] == "I'll check the file"
    assert summary("cancelled")["report"] is None  # its last words, not a report
    assert (summary("failed")["report"], summary("failed")["error"]) == (None, "boom")
    assert summary("completed", live=True)["report"] is None
    # A run suspended on a card has a finish time, and resumes.
    assert LatestRun("waiting", 1.0, 5.0, None).seconds is None
    assert LatestRun("cancelled", 1.0, 5.0, None).seconds == 4.0


async def test_a_failed_tasks_summary_says_why() -> None:
    import httpx

    child = Agent(name="doomed", model=ScriptedProvider([call("missing_tool", {})]))
    provider = ScriptedProvider(
        [call("spawn_subagent", {"prompt": "fail"}), text("bye"), text("noted")]
    )
    agent = Agent(name="bot", model=provider, plugins=[Subagents(child, max_turns=1)])
    app = _app(agent)
    deps = app.state.deps
    await deps.store.upsert("s9", agent="bot", title="chat")
    await deps.supervisor.start(
        session_id="s9",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="user",
    )
    task = await _task_of(deps, "s9")

    async def failed() -> bool:
        last = (await deps.store.latest_runs([task.id])).get(task.id)
        return last is not None and last.status == "failed"

    await _poll(failed)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        summary = (await c.get(f"/api/sessions/{task.id}/summary")).json()
    assert summary["error"] and summary["failed_calls"] == 1


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
        run = await deps.store.latest_run_for("subagent-report:t1")
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
        from lovia.web.store import RunRow

        await deps.store.upsert("chat", agent="bot", title="chat")
        await deps.store.upsert("task", agent="bot", title="[t1] x", parent_id="chat")
        await deps.store.start_run(
            RunRow(
                id="r1",
                session_id="task",
                agent="bot",
                source="subagent:task",
                status="running",
                error=None,
                started_at=1.0,
                finished_at=None,
                usage=None,
            )
        )
        await deps.store.finish_run("r1", status="completed", error=None, usage=None)

    asyncio.run(seed())
    client = TestClient(app)
    rows = {r["id"]: r for r in client.get("/api/sessions").json()}
    assert rows["chat"]["parent_id"] is None
    assert rows["chat"]["last_run_status"] is None
    # Task sessions are excluded from the global list by construction.
    assert "task" not in rows
    # ?parent= scopes to one chat's tasks — the topbar popover's data.
    kids = client.get("/api/sessions", params={"parent": "chat"}).json()
    assert [k["id"] for k in kids] == ["task"]
    assert kids[0]["last_run_status"] == "completed"
    assert client.get("/api/sessions", params={"parent": "nope"}).json() == []


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
    # The global list is chats-only; children come from list_children.
    assert [r.id for r in await store.list()] == ["old"]
    assert [r.id for r in await store.list_children("old")] == ["kid"]
