"""create_app(run_context=...): the page's client context and the request's
identity reach a run's tools as RunContext.deps — on every way a run starts."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException, Request  # noqa: E402

from lovia import Agent, RunContext, tool  # noqa: E402
from lovia.web import RunTrigger, create_app, subagent_deliver  # noqa: E402
from lovia.web.scheduler import Scheduler  # noqa: E402
from lovia.web.store import ChatStore, ScheduleRow  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_scheduler import _drain_runs  # noqa: E402
from .test_supervisor import (  # noqa: E402
    _blocking_tool,
    _client,
    _spawn,
    _wait_record,
)
from .test_web import _parse_sse  # noqa: E402

seen: list[RunTrigger] = []


@tool
async def whoami(ctx: RunContext) -> str:
    """Report the run's deps."""
    return repr(ctx.deps)


def _agent(script) -> Agent:
    return Agent(name="bot", model=ScriptedProvider(script), tools=[whoami])


def _build(trigger: RunTrigger) -> dict:
    seen.append(trigger)
    user = getattr(trigger.request.state, "user", None) if trigger.request else None
    return {"user": user, "kb": trigger.client.get("kb"), "source": trigger.source}


async def _auth(request: Request) -> None:
    request.state.user = request.headers.get("x-user", "anon")


def _app(agent: Agent, *, store: ChatStore | None = None, **kw):
    seen.clear()
    kw.setdefault("run_context", _build)
    return create_app(
        agent,
        store=store or ChatStore.in_memory(),
        generate_titles=False,
        auth=_auth,
        **kw,
    )


def _tool_result(events) -> str:
    return next(d["result"] for e, d in events if e == "tool_result")


async def _stream(app, body, *, headers=None):
    async with _client(app) as ac:
        res = await ac.post("/api/chat/stream", json=body, headers=headers or {})
        return _parse_sse(res.text)


async def test_stream_hands_client_context_and_identity_to_tools() -> None:
    app = _app(_agent([call("whoami", {}), text("ok")]))
    events = await _stream(
        app,
        {"message": "hi", "context": {"kb": "docs"}},
        headers={"x-user": "ada"},
    )
    assert _tool_result(events) == repr({"user": "ada", "kb": "docs", "source": "user"})
    [trigger] = seen
    assert trigger.client == {"kb": "docs"}
    assert trigger.resumed is False
    assert trigger.agent.name == "bot"


async def test_blocking_chat_hands_it_over_too() -> None:
    app = _app(_agent([call("whoami", {}), text("ok")]))
    async with _client(app) as ac:
        res = await ac.post(
            "/api/chat",
            json={"message": "hi", "context": {"kb": "tickets"}},
            headers={"x-user": "bo"},
        )
        assert res.status_code == 200
        detail = (await ac.get(f"/api/sessions/{res.json()['session_id']}")).json()
    contents = " ".join(str(m["content"]) for m in detail["entries"])
    assert repr({"user": "bo", "kb": "tickets", "source": "user"}) in contents


async def test_an_async_run_context_is_awaited() -> None:
    async def build(trigger: RunTrigger) -> str:
        return f"async:{trigger.client.get('kb')}"

    app = _app(_agent([call("whoami", {}), text("ok")]), run_context=build)
    events = await _stream(app, {"message": "hi", "context": {"kb": "docs"}})
    assert _tool_result(events) == repr("async:docs")


async def test_without_run_context_runs_get_none_and_a_context_is_flagged(
    caplog,
) -> None:
    import lovia.web.run_context as rc

    rc._warned_unused = False
    app = _app(_agent([call("whoami", {}), text("ok")]), run_context=None)
    with caplog.at_level(logging.WARNING, logger="lovia.web.run_context"):
        events = await _stream(app, {"message": "hi", "context": {"kb": "docs"}})
    assert _tool_result(events) == "None"
    assert "no run_context=" in caplog.text


async def test_an_oversized_context_is_refused() -> None:
    app = _app(_agent([text("ok")]))
    async with _client(app) as ac:
        res = await ac.post(
            "/api/chat/stream",
            json={"message": "hi", "context": {"blob": "x" * (65 * 1024)}},
        )
    assert res.status_code == 422


async def test_the_live_run_keeps_its_client_beside_the_pointer() -> None:
    store = ChatStore.in_memory()

    @tool
    async def stored(ctx: RunContext) -> str:
        """Read the run's persisted client context."""
        return repr(await store.get_active_context(ctx.session_id))

    agent = Agent(
        name="bot",
        model=ScriptedProvider([call("stored", {}), text("ok")]),
        tools=[stored],
    )
    app = _app(agent, store=store)
    events = await _stream(app, {"message": "hi", "context": {"kb": "docs"}})
    assert _tool_result(events) == repr({"kb": "docs"})
    sid = next(d["session_id"] for e, d in events if e == "session")
    await _drain_runs(app.state.deps)
    assert await store.get_active_context(sid) is None  # gone with the run
    assert app.state.deps.supervisor.last_client[sid] == {"kb": "docs"}


async def test_a_resume_gets_the_runs_client_and_the_reconnecting_request() -> None:
    from lovia.checkpointer import RunHead
    from lovia.messages import Usage
    from lovia.transcript import InputEntry

    store = ChatStore.in_memory()
    sid = "s1"
    await store.upsert(sid, agent="bot")
    await store.checkpointer.append(
        "run-1",
        [InputEntry(role="user", content="q")],
        RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
    )
    # As the run left it before the restart.
    await store.set_active_run_id(sid, "run-1", context={"kb": "docs"})

    app = _app(_agent([call("whoami", {}), text("ok")]), store=store)
    async with _client(app) as ac:
        res = await ac.post(
            "/api/chat/reconnect", params={"session_id": sid}, headers={"x-user": "cy"}
        )
    events = _parse_sse(res.text)
    assert _tool_result(events) == repr({"user": "cy", "kb": "docs", "source": "user"})
    assert seen[-1].resumed is True
    await _wait_record(store, "run-1")


async def test_a_schedule_fires_with_the_context_it_was_made_with() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([call("whoami", {}), text("ok")]), store=store)
    deps = app.state.deps
    async with _client(app) as ac:
        res = await ac.post(
            "/api/schedules",
            json={
                "input": "daily digest",
                "trigger_kind": "every",
                "trigger_expr": "3600",
                "context": {"kb": "digest"},
            },
        )
    assert res.status_code == 200, res.text
    row = (await store.list_schedules())[0]
    assert row.context == {"kb": "digest"}
    # Fire it now.
    await store.add_schedule(
        ScheduleRow(**{**row.__dict__, "id": "due", "next_fire": time.time() - 1})
    )
    await store.delete_schedule(row.id)
    await Scheduler(deps).run_due()
    await _drain_runs(deps)
    fired = (await store.get_schedule("due")).last_session_id
    assert fired is not None
    outputs = [getattr(e, "output", "") for e in await store.session.load(fired)]
    # Nobody requested a scheduled fire: no request, so no identity.
    assert repr({"user": None, "kb": "digest", "source": "schedule:due"}) in outputs


async def test_a_schedule_the_agent_makes_keeps_its_runs_client() -> None:
    from lovia.web.run_context import RUN_CLIENT

    from .test_scheduling_tool import _invoke

    store = ChatStore.in_memory()
    token = RUN_CLIENT.set({"kb": "docs"})  # as the supervisor sets it per run
    try:
        await _invoke(
            store,
            {
                "instruction": "check again",
                "trigger_kind": "every",
                "trigger_expr": "300",
            },
        )
    finally:
        RUN_CLIENT.reset(token)
    [row] = await store.list_schedules()
    assert row.context == {"kb": "docs"}


async def test_a_report_run_continues_in_its_chats_context() -> None:
    from lovia.plugins.subagents import SubagentReport

    app = _app(_agent([call("whoami", {}), text("ok")]))
    deps = app.state.deps
    await deps.store.upsert("s2", agent="bot")
    deps.supervisor.last_client["s2"] = {"kb": "docs"}

    report = SubagentReport(
        id="t1", agent="bot", prompt="p", session_id="s2", result=None, error=None,
        text="[subagent t1: done] news",
    )  # fmt: skip
    await subagent_deliver(deps)(report)
    await _drain_runs(deps)
    assert seen[-1].source == "subagent-report:t1"
    assert seen[-1].client == {"kb": "docs"}
    assert seen[-1].request is None


def _refusing(trigger: RunTrigger) -> dict:
    """Refuse any run whose page picked the "secret" knowledge base."""
    if trigger.client.get("kb") == "secret":
        raise HTTPException(403, "not your knowledge base")
    return _build(trigger)


async def test_a_refused_turn_leaves_the_interrupted_run_resumable() -> None:
    from lovia.checkpointer import RunHead
    from lovia.messages import Usage
    from lovia.transcript import InputEntry

    store = ChatStore.in_memory()
    sid = "s1"
    await store.upsert(sid, agent="bot")
    await store.checkpointer.append(
        "run-1",
        [InputEntry(role="user", content="q")],
        RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
    )
    await store.set_active_run_id(sid, "run-1", context={"kb": "docs"})
    app = _app(_agent([text("ok")]), store=store, run_context=_refusing)
    async with _client(app) as ac:
        body = {"message": "hi", "session_id": sid, "context": {"kb": "secret"}}
        res = await ac.post("/api/chat/stream", json=body)
        assert res.status_code == 403
        assert await store.get_active_run_id(sid) == "run-1"
        assert await store.checkpointer.load("run-1") is not None
        # A turn that does start supersedes it.
        body["context"] = {"kb": "docs"}
        await ac.post("/api/chat/stream", json=body)
    assert await store.checkpointer.load("run-1") is None


async def test_a_refused_new_chat_leaves_no_trace(monkeypatch) -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("ok")]), store=store, run_context=_refusing)
    created: list[str] = []
    monkeypatch.setattr(app.state.deps, "emit", lambda event, _: created.append(event))
    async with _client(app) as ac:
        for path in ("/api/chat/stream", "/api/chat"):
            body = {"message": "hi", "session_id": "new", "context": {"kb": "secret"}}
            assert (await ac.post(path, json=body)).status_code == 403
    assert await store.get("new") is None
    assert "session_created" not in created


async def test_a_blocking_turn_hands_its_context_to_the_tasks_it_spawns() -> None:
    app = _app(_agent([text("ok")]))
    async with _client(app) as ac:
        res = await ac.post("/api/chat", json={"message": "hi", "context": {"kb": "b"}})
    sid = res.json()["session_id"]
    assert app.state.deps.supervisor.last_client[sid] == {"kb": "b"}


async def test_a_refused_schedule_is_a_miss_and_the_next_one_still_fires() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("ok")]), store=store, run_context=_refusing)
    deps = app.state.deps
    now = time.time()
    for sid, kb, due in (("bad", "secret", now - 2), ("good", "docs", now - 1)):
        await store.add_schedule(
            ScheduleRow(
                id=sid, agent="bot", input="digest", session_id=None,
                trigger_kind="every", trigger_expr="3600", next_fire=due,
                active=True, last_session_id=None, created_at=now,
                updated_at=now, context={"kb": kb},
            )
        )  # fmt: skip
    await Scheduler(deps).run_due()
    await _drain_runs(deps)
    bad, good = await store.get_schedule("bad"), await store.get_schedule("good")
    assert bad.next_fire > now and bad.last_session_id is None
    assert good.last_session_id is not None
    [miss] = await store.list_runs(source="schedule:bad")
    assert miss.status == "failed" and "not your knowledge base" in miss.error
    # No empty chat left behind by the refused fire.
    assert [s.id for s in await store.list(limit=10)] == [good.last_session_id]


async def test_a_refused_subagent_task_leaves_no_task_chat() -> None:
    from lovia.plugins.subagents import ChildSpec
    from lovia.reliability import CancelToken
    from lovia.steering import Mailbox
    from lovia.web.subagents import subagent_runner

    app = _app(_agent([text("ok")]), run_context=_refusing)
    deps = app.state.deps
    await deps.store.upsert("parent", agent="bot")
    deps.supervisor.last_client["parent"] = {"kb": "secret"}
    spec = ChildSpec(
        id="t1", agent=_agent([text("ok")]), prompt="dig", token=CancelToken(),
        parent_session_id="parent", max_turns=5, budget=None, mailbox=Mailbox(),
    )  # fmt: skip
    with pytest.raises(HTTPException):
        await subagent_runner(deps)(spec)
    assert await deps.store.list_children("parent") == []


async def _until(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met"
        await asyncio.sleep(0.01)


async def test_a_start_holds_its_place_while_the_hook_decides(tmp_path) -> None:
    """From the first check on, a starting run holds its chat and a place under
    the run cap, through the hook: a second request for that chat attaches to
    it rather than racing it, one for another chat is turned away before it
    opens a workspace, and the refusal leaves no chat behind."""
    from lovia.workspace import Workspace

    gate = asyncio.Event()

    async def slow_refusal(trigger: RunTrigger) -> None:
        if trigger.client.get("kb") == "secret":
            await gate.wait()
            raise HTTPException(403, "not your knowledge base")

    agent = Agent(
        name="bot",
        model=ScriptedProvider([text("ok")]),
        workspace=Workspace.local(str(tmp_path)),
    )
    store = ChatStore.in_memory()
    app = _app(agent, store=store, run_context=slow_refusal, max_background_runs=1)
    deps = app.state.deps
    async with _client(app) as ac:
        body = {"message": "a", "session_id": "new", "context": {"kb": "secret"}}
        refused = asyncio.create_task(ac.post("/api/chat/stream", json=body))
        await _until(lambda: deps.supervisor.get("new") is not None)
        pending = deps.supervisor.get("new")
        same = asyncio.create_task(
            ac.post("/api/chat/stream", json={"message": "b", "session_id": "new"})
        )
        await _until(lambda: bool(pending.hub._subs))  # attached, not started
        other = await ac.post(
            "/api/chat/stream", json={"message": "c", "session_id": "other"}
        )
        assert other.status_code == 429
        assert deps.workspaces.get("other") is None
        gate.set()
        assert (await refused).status_code == 403
        # Its stream ends with the refused start, having sent no snapshot:
        # the run's view wasn't loaded, and an empty one would wipe the page.
        attached = _parse_sse((await asyncio.wait_for(same, 5)).text)
        assert "snapshot" not in [e for e, _ in attached]
        assert await store.get("new") is None
        assert deps.workspaces.get("new") is None
        # The chat is free again, and an accepted turn makes it for good.
        await ac.post("/api/chat/stream", json={"message": "d", "session_id": "new"})
    assert (await store.get("new")).title == "d"


async def test_a_failed_setup_closes_only_the_workspace_it_opened(
    tmp_path, monkeypatch
) -> None:
    from lovia.workspace import Workspace

    agent = Agent(
        name="bot",
        model=ScriptedProvider([text("ok")]),
        workspace=Workspace.local(str(tmp_path)),
    )
    app = _app(agent)
    deps = app.state.deps

    async def broken(*_a, **_k) -> None:
        raise RuntimeError("store down")

    monkeypatch.setattr(deps.store, "set_active_run_id", broken)
    start = {"agent": agent, "input": "hi", "is_new": False, "title_message": None}
    with pytest.raises(RuntimeError):
        await deps.supervisor.start(session_id="fresh", **start)
    assert deps.workspaces.get("fresh") is None
    # A chat whose workspace an earlier run opened keeps it (and its processes).
    await deps.workspaces.bind("used", agent)
    with pytest.raises(RuntimeError):
        await deps.supervisor.start(session_id="used", **start)
    assert deps.workspaces.get("used") is not None
    await deps.workspaces.aclose()


def _gated(gate: asyncio.Event, *, when):
    """A run_context hook that holds the runs ``when(trigger)`` picks at
    ``gate`` — a setup kept waiting while the test acts around it."""

    async def hook(trigger: RunTrigger) -> None:
        if when(trigger):
            await gate.wait()

    return hook


@pytest.mark.parametrize("stop", ["cancel", "delete"])
async def test_a_stop_during_setup_beats_the_stale_start(stop: str) -> None:
    """A chat stopped (or deleted) while a start's hook still waits, then taken
    by a replacement turn: the stale start gives up and touches nothing of
    the replacement's — not its controller, not its checkpoint pointer."""
    gate, release = asyncio.Event(), asyncio.Event()
    agent = _agent([call("block", {}), text("done")])
    agent = agent.clone(tools=[_blocking_tool(release)])
    store = ChatStore.in_memory()
    await store.upsert("s", agent="bot")
    hook = _gated(gate, when=lambda t: t.client.get("kb") == "slow")
    app = _app(agent, store=store, run_context=hook)
    sup = app.state.deps.supervisor
    async with _client(app) as ac:
        body = {"message": "a", "session_id": "s", "context": {"kb": "slow"}}
        stale = asyncio.create_task(ac.post("/api/chat/stream", json=body))
        await _until(lambda: sup.get("s") is not None)
        stale_ctrl = sup.get("s")
        if stop == "cancel":
            await ac.post("/api/chat/cancel", params={"session_id": "s"})
        else:
            await ac.delete("/api/sessions/s")
        replacement, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "b", "session_id": "s"}
        )
        await _until(
            lambda: (
                (c := sup.get("s")) is not None
                and c is not stale_ctrl
                and c.task is not None
            )
        )
        live = sup.get("s")
        gate.set()
        res = await stale
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "run_cancelled"
        assert sup.get("s") is live
        assert await store.get_active_run_id("s") == live.run_id
        release.set()
        await asyncio.wait_for(replacement, 5)
    entries = await store.session.load("s")
    assert [e.content for e in entries if getattr(e, "role", None) == "user"] == ["b"]


async def test_a_blocking_turn_holds_its_chat_through_the_hook() -> None:
    gate = asyncio.Event()
    hook = _gated(gate, when=lambda t: t.client.get("kb") == "slow")
    store = ChatStore.in_memory()
    agent = _agent([text("blocking"), text("stream")])
    app = _app(agent, store=store, run_context=hook)
    sup = app.state.deps.supervisor
    async with _client(app) as ac:
        body = {"message": "a", "session_id": "s", "context": {"kb": "slow"}}
        blocking = asyncio.create_task(ac.post("/api/chat", json=body))
        await _until(lambda: "s" in sup._reserved)
        then = {"message": "b", "session_id": "s"}
        assert (await ac.post("/api/chat/stream", json=then)).status_code == 409
        gate.set()
        assert (await blocking).status_code == 200
        assert "s" not in sup._reserved  # released with the turn
        assert (await ac.post("/api/chat/stream", json=then)).status_code == 200
    entries = await store.session.load("s")
    users = [e.content for e in entries if getattr(e, "role", None) == "user"]
    assert users == ["a", "b"]


@pytest.mark.parametrize("delete", ["one", "all"])
async def test_deleting_a_chat_during_a_blocking_turns_hook_wins(delete: str) -> None:
    gate = asyncio.Event()
    provider = ScriptedProvider([text("late")])
    hook = _gated(gate, when=lambda t: t.client.get("kb") == "slow")
    store = ChatStore.in_memory()
    await store.upsert("s", agent="bot")
    app = _app(Agent(name="bot", model=provider), store=store, run_context=hook)
    deps = app.state.deps
    async with _client(app) as ac:
        body = {"message": "a", "session_id": "s", "context": {"kb": "slow"}}
        blocking = asyncio.create_task(ac.post("/api/chat", json=body))
        await _until(lambda: "s" in deps.supervisor._reserved)
        await ac.delete("/api/sessions/s" if delete == "one" else "/api/sessions")
        gate.set()
        res = await blocking
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "run_cancelled"
    assert provider.calls == []
    assert await store.get("s") is None
    assert "s" not in deps.supervisor.last_client


async def test_stopping_a_blocking_turn_in_its_hook_stops_it() -> None:
    gate = asyncio.Event()
    provider = ScriptedProvider([text("late")])
    hook = _gated(gate, when=lambda t: t.client.get("kb") == "slow")
    app = _app(Agent(name="bot", model=provider), run_context=hook)
    sup = app.state.deps.supervisor
    async with _client(app) as ac:
        body = {"message": "a", "session_id": "s", "context": {"kb": "slow"}}
        blocking = asyncio.create_task(ac.post("/api/chat", json=body))
        await _until(lambda: "s" in sup._reserved)
        await ac.post("/api/chat/cancel", params={"session_id": "s"})
        assert "s" in sup._reserved  # still held: the turn isn't over yet
        gate.set()
        res = await blocking
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "run_cancelled"
    assert provider.calls == []
    assert "s" not in sup._reserved


async def test_stopping_a_running_blocking_turn_stops_its_runner() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}), text("never")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent)
    sup = app.state.deps.supervisor
    async with _client(app) as ac:
        blocking = asyncio.create_task(
            ac.post("/api/chat", json={"message": "a", "session_id": "s"})
        )
        await _until(lambda: len(provider.calls) == 1)  # inside the tool
        await ac.post("/api/chat/cancel", params={"session_id": "s"})
        release.set()
        res = await asyncio.wait_for(blocking, 5)
    assert res.status_code == 409
    assert len(provider.calls) == 1  # stopped before its next model call
    assert "s" not in sup._reserved


async def test_a_setup_pending_at_shutdown_gives_up() -> None:
    from lovia.web.errors import WebError

    gate = asyncio.Event()
    store = ChatStore.in_memory()
    agent = _agent([text("ok")])
    app = _app(agent, store=store, run_context=_gated(gate, when=lambda t: True))
    sup = app.state.deps.supervisor
    start = asyncio.create_task(
        sup.start(
            session_id="s", agent=agent, input="hi", is_new=True,
            title_message=None,
            on_admit=lambda: store.upsert("s", agent="bot", title="hi"),
        )
    )  # fmt: skip
    await _until(lambda: sup.get("s") is not None)
    await sup.shutdown(grace=0.1)
    gate.set()
    with pytest.raises(WebError) as exc:
        await start
    assert exc.value.code == "run_cancelled"
    assert sup.get("s") is None
    assert await store.get("s") is None


async def test_a_schedule_waits_out_a_blocking_turn_and_the_next_one_fires() -> None:
    gate = asyncio.Event()
    hook = _gated(gate, when=lambda t: t.client.get("kb") == "slow")
    store = ChatStore.in_memory()
    await store.upsert("busy", agent="bot")
    app = _app(_agent([text("ok")] * 3), store=store, run_context=hook)
    deps = app.state.deps
    now = time.time()
    for sid, target, due in (("fixed", "busy", now - 2), ("fresh", None, now - 1)):
        await store.add_schedule(
            ScheduleRow(
                id=sid, agent="bot", input="digest", session_id=target,
                trigger_kind="every", trigger_expr="3600", next_fire=due,
                active=True, last_session_id=None, created_at=now,
                updated_at=now,
            )
        )  # fmt: skip
    async with _client(app) as ac:
        body = {"message": "a", "session_id": "busy", "context": {"kb": "slow"}}
        blocking = asyncio.create_task(ac.post("/api/chat", json=body))
        await _until(lambda: "busy" in deps.supervisor._reserved)
        await Scheduler(deps).run_due()
        gate.set()
        assert (await blocking).status_code == 200
    await _drain_runs(deps)
    fixed, fresh = await store.get_schedule("fixed"), await store.get_schedule("fresh")
    assert fixed.next_fire == now - 2  # still due: the next poll fires it
    assert await store.list_runs(source="schedule:fixed") == []
    assert fresh.last_session_id is not None


async def _interrupted(store: ChatStore, sid: str = "s1") -> None:
    from lovia.checkpointer import RunHead
    from lovia.messages import Usage
    from lovia.transcript import InputEntry

    await store.upsert(sid, agent="bot")
    await store.checkpointer.append(
        "run-1",
        [InputEntry(role="user", content="q")],
        RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
    )
    await store.set_active_run_id(sid, "run-1", context={"kb": "docs"})


async def test_concurrent_reconnects_resume_the_run_once() -> None:
    gate = asyncio.Event()
    provider = ScriptedProvider([text("resumed"), text("again")])
    store = ChatStore.in_memory()
    await _interrupted(store)
    hook = _gated(gate, when=lambda t: t.resumed)
    app = _app(Agent(name="bot", model=provider), store=store, run_context=hook)
    sup = app.state.deps.supervisor
    async with _client(app) as ac:
        params = {"session_id": "s1"}
        first, first_lines = _spawn(ac, "/api/chat/reconnect", params=params)
        await _until(lambda: sup.get("s1") is not None)
        pending = sup.get("s1")
        second, second_lines = _spawn(ac, "/api/chat/reconnect", params=params)
        await _until(lambda: bool(pending.hub._subs))  # attached to the first
        gate.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert len(provider.calls) == 1
    # Both see the history the run was interrupted in — the second, attached
    # mid-setup, too: an empty snapshot would wipe the page.
    for lines in (first_lines, second_lines):
        snapshot = next(d for e, d in _parse_sse("\n".join(lines)) if e == "snapshot")
        assert [m["content"] for m in snapshot["entries"]] == ["q"]


async def test_a_snapshot_another_resume_moved_on_from_is_not_resumed() -> None:
    from lovia.web.errors import WebError

    store = ChatStore.in_memory()
    await _interrupted(store)
    agent = _agent([text("resumed")])
    app = _app(agent, store=store)
    sup = app.state.deps.supervisor
    snapshot = await store.checkpointer.load("run-1")
    resume = {"session_id": "s1", "agent": agent, "snapshot": snapshot, "history": []}
    ctrl = await sup.start_resume(**resume)
    ctrl._ensure_begun()
    await ctrl.task  # resumed and finished: checkpoint and pointer are gone
    with pytest.raises(WebError) as exc:
        await sup.start_resume(**resume)
    assert exc.value.code == "run_not_found"


async def test_a_stop_during_resume_setup_drops_the_run() -> None:
    gate = asyncio.Event()
    provider = ScriptedProvider([text("resumed")])
    store = ChatStore.in_memory()
    await _interrupted(store)
    hook = _gated(gate, when=lambda t: t.resumed)
    app = _app(Agent(name="bot", model=provider), store=store, run_context=hook)
    sup = app.state.deps.supervisor
    async with _client(app) as ac:
        params = {"session_id": "s1"}
        resume = asyncio.create_task(ac.post("/api/chat/reconnect", params=params))
        await _until(lambda: sup.get("s1") is not None)
        await ac.post("/api/chat/cancel", params=params)
        gate.set()
        res = await resume
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "run_cancelled"
    assert provider.calls == []
    assert await store.get_active_run_id("s1") is None
    assert await store.checkpointer.load("run-1") is None
