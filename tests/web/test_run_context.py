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
from .test_supervisor import _client, _wait_record  # noqa: E402
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
        await asyncio.wait_for(same, 5)  # its stream ends with the refused start
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
