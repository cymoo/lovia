"""create_app(run_context=...): the page's client context and the request's
identity reach a run's tools as RunContext.deps — on every way a run starts."""

from __future__ import annotations

import logging
import time

import pytest

pytest.importorskip("fastapi")

from fastapi import Request  # noqa: E402

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
