"""Runs suspended on an approval nobody answered in time (``suspend_after``).

The run stops at the call and frees its slot; the card stays, and answering
it — from any tab, after any delay, from another app on the same store —
resumes the run with that answer. A message joins it, a stop settles it, a
report waits for it, and nothing starts a fresh run over it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

pytest.importorskip("fastapi")

from lovia import (  # noqa: E402
    Agent,
    CancelToken,
    Mailbox,
    RunCancelled,
    RunSuspended,
    tool,
)
from lovia.plugins.subagents import ChildSpec  # noqa: E402
from lovia.tools import HumanChannel, ask_human  # noqa: E402
from lovia.web import subagent_deliver  # noqa: E402
from lovia.web.subagents import subagent_runner  # noqa: E402
from lovia.web.scheduler import Scheduler  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_scheduler import _row  # noqa: E402
from .test_subagents import _report  # noqa: E402
from .test_supervisor import _client, _spawn, _wait_run  # noqa: E402
from .test_web import _app, _parse_sse  # noqa: E402


def _agent(provider: ScriptedProvider, runs: list[str]) -> Agent[Any]:
    @tool(permission="ask")
    async def push() -> str:
        """Push to the remote."""
        runs.append("push")
        return "pushed"

    return Agent(name="bot", model=provider, tools=[push])


async def _suspend(ac: Any, sid: str = "s1") -> list[tuple[str, Any]]:
    """Send a message whose run asks for ``push`` and let the card go
    unanswered: the stream ends at the suspension."""
    task, lines = _spawn(
        ac, "/api/chat/stream", json={"message": "go", "session_id": sid}
    )
    await asyncio.wait_for(task, timeout=5)
    await _wait_run(ac, sid, status="waiting")
    return _parse_sse("\n".join(lines))


async def _record(ac: Any, sid: str) -> dict[str, Any]:
    (record,) = (await ac.get("/api/runs/history", params={"session_id": sid})).json()
    return record


def _texts(provider: ScriptedProvider) -> list[str]:
    """What the model was sent on its last call, as plain text."""
    return [str(m.content) for m in provider.calls[-1]]


@pytest.mark.asyncio
async def test_an_unanswered_card_suspends_the_run() -> None:
    runs: list[str] = []
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    app = _app(_agent(provider, runs), suspend_after=0.2)
    async with _client(app) as ac:
        evs = await _suspend(ac)
        kinds = [e for e, _ in evs]
        # The card, then word that the run waits on it: no error, no done.
        assert kinds.index("approval_required") < kinds.index("suspended")
        assert "done" not in kinds and "error" not in kinds
        assert dict(evs)["suspended"] == {"id": "c1"}

        (row,) = (await ac.get("/api/runs")).json()
        assert row["status"] == "waiting" and row["pending_approval"]["id"] == "c1"
        detail = (await ac.get("/api/sessions/s1")).json()
        # The card instead of a reconnect pointer: opening the chat must not
        # resume the run.
        assert detail["active_run_id"] is None
        assert detail["waiting"]["id"] == "c1"
        assert detail["waiting"]["name"] == "push"
        res = await ac.post("/api/chat/reconnect", params={"session_id": "s1"})
        assert res.status_code == 409 and res.json()["detail"]["code"] == "run_waiting"
        assert (await _record(ac, "s1"))["status"] == "waiting"
    assert runs == []


@pytest.mark.asyncio
async def test_answering_the_card_resumes_the_run() -> None:
    runs: list[str] = []
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    app = _app(_agent(provider, runs), suspend_after=0.2)
    async with _client(app) as ac:
        await _suspend(ac)
        res = await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "approve"},
        )
        assert res.json() == {"ok": True, "remembered": False, "resumed": True}
        await _wait_run(ac, "s1", gone=True)
        # A second answer finds nothing to decide.
        again = await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "approve"},
        )
        assert again.status_code == 404
        assert (await _record(ac, "s1"))["status"] == "completed"
        detail = (await ac.get("/api/sessions/s1")).json()
        assert detail["waiting"] is None and detail["active_run_id"] is None
    assert runs == ["push"]  # once, on the resume


@pytest.mark.asyncio
async def test_a_denial_reaches_the_model_on_resume() -> None:
    runs: list[str] = []
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ok")])
    app = _app(_agent(provider, runs), suspend_after=0.2)
    async with _client(app) as ac:
        await _suspend(ac)
        res = await ac.post(
            "/api/chat/approve",
            json={
                "session_id": "s1",
                "call_id": "c1",
                "decision": "deny",
                "reason": "open a PR instead",
            },
        )
        assert res.json()["resumed"] is True
        await _wait_run(ac, "s1", gone=True)
    assert runs == []
    assert any("not approved: open a PR instead" in t for t in _texts(provider))


@pytest.mark.asyncio
async def test_a_message_joins_the_waiting_run() -> None:
    runs: list[str] = []
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    app = _app(_agent(provider, runs), suspend_after=5)
    app.state.deps.suspend_after = 0.2
    async with _client(app) as ac:
        await _suspend(ac)
        app.state.deps.suspend_after = 5  # the card asked again waits live
        task, lines = _spawn(
            ac,
            "/api/chat/stream",
            json={"message": "and tag it", "session_id": "s1"},
        )
        await _wait_run(ac, "s1", status="blocked_on_approval")
        await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "approve"},
        )
        await asyncio.wait_for(task, timeout=5)
    kinds = [e for e, _ in _parse_sse("\n".join(lines))]
    assert kinds[0] == "session" and "snapshot" in kinds and kinds[-1] == "done"
    assert runs == ["push"]
    assert "and tag it" in _texts(provider)  # read once the card was answered


@pytest.mark.asyncio
async def test_a_stop_settles_the_waiting_run() -> None:
    runs: list[str] = []
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("fresh start")])
    store = ChatStore.in_memory()
    app = _app(_agent(provider, runs), suspend_after=0.2, store=store)
    async with _client(app) as ac:
        await _suspend(ac)
        # A report that waited with the run stays in the chat after the stop.
        await subagent_deliver(app.state.deps)(_report("s1", text="[subagent t1] news"))
        res = await ac.post("/api/chat/cancel", params={"session_id": "s1"})
        assert res.json() == {"ok": True}
        assert (await _record(ac, "s1"))["status"] == "cancelled"
        assert (await ac.get("/api/runs")).json() == []
        detail = (await ac.get("/api/sessions/s1")).json()
        assert detail["waiting"] is None and detail["active_run_id"] is None
        # The chat keeps what it showed; the card's call is recorded as not run.
        entries = await store.session.load("s1")
        assert any(
            "waited for approval" in str(getattr(e, "output", "")) for e in entries
        )
        assert any(getattr(e, "content", None) == "[subagent t1] news" for e in entries)
        assert await store.get_active_run_id("s1") is None
        # The chat is free again: a message starts a run of its own.
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "again", "session_id": "s1"}
        )
        await asyncio.wait_for(task, timeout=5)
    assert runs == []


@pytest.mark.asyncio
async def test_a_run_suspended_after_a_handoff_resumes() -> None:
    # The acting agent is a handoff target, served under no name of its own:
    # the resume starts from the chat's agent, as its record names it.
    runs: list[str] = []
    provider = ScriptedProvider(
        [
            call("transfer_to_billing", {}, call_id="h1"),
            call("push", {}, call_id="c1"),
            text("ack"),
        ]
    )
    billing = _agent(provider, runs).clone(name="billing")
    agent = Agent(name="bot", model=provider, handoffs=[billing])
    app = _app(agent, suspend_after=0.2)
    async with _client(app) as ac:
        await _suspend(ac)
        res = await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "approve"},
        )
        assert res.status_code == 200 and res.json()["resumed"] is True
        await _wait_run(ac, "s1", gone=True)
    assert runs == ["push"]


@pytest.mark.asyncio
async def test_nothing_starts_a_fresh_run_over_a_waiting_one() -> None:
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    app = _app(_agent(provider, []), suspend_after=0.2)
    async with _client(app) as ac:
        await _suspend(ac)
        blocking = await ac.post(
            "/api/chat", json={"message": "hi", "session_id": "s1"}
        )
        assert blocking.status_code == 409
        assert blocking.json()["detail"]["code"] == "run_waiting"
        queued = await ac.post(
            "/api/chat/stream",
            json={"message": "later", "session_id": "s1", "busy": "reject"},
        )
        assert queued.status_code == 409
        assert queued.json()["detail"]["code"] == "run_waiting"
        assert (await _record(ac, "s1"))["status"] == "waiting"


@pytest.mark.asyncio
async def test_a_report_waits_with_the_run() -> None:
    runs: list[str] = []
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    app = _app(_agent(provider, runs), suspend_after=0.2)
    deps = app.state.deps
    async with _client(app) as ac:
        await _suspend(ac)
        await subagent_deliver(deps)(_report("s1", text="[subagent t1: done] news"))
        # Queued, not run: the chat still waits on its card.
        (row,) = (await ac.get("/api/runs")).json()
        assert row["status"] == "waiting"
        record = await deps.store.waiting_run("s1")
        assert record is not None and len(record.waiting_for["queued"]) == 1
        await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "approve"},
        )
        await _wait_run(ac, "s1", gone=True)
    assert runs == ["push"]
    assert "[subagent t1: done] news" in _texts(provider)


@pytest.mark.asyncio
async def test_the_card_outlives_the_app() -> None:
    # Another app on the same store (a restart): the card is still offered,
    # and answering it resumes the run there.
    runs: list[str] = []
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    store = ChatStore.in_memory()
    first = _app(_agent(provider, runs), suspend_after=0.2, store=store)
    async with _client(first) as ac:
        await _suspend(ac)
    second = _app(_agent(provider, runs), store=store)
    async with _client(second) as ac:
        detail = (await ac.get("/api/sessions/s1")).json()
        assert detail["waiting"]["id"] == "c1"
        res = await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "approve"},
        )
        assert res.json()["resumed"] is True
        await _wait_run(ac, "s1", gone=True)
    assert runs == ["push"]


@pytest.mark.asyncio
async def test_a_waiting_run_without_its_checkpoint_frees_the_chat() -> None:
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("fresh")])
    store = ChatStore.in_memory()
    app = _app(_agent(provider, []), suspend_after=0.2, store=store)
    async with _client(app) as ac:
        await _suspend(ac)
        # Its checkpoint went (as a rewind past it does): nothing to resume.
        run_id = await store.get_active_run_id("s1")
        assert run_id is not None and store.checkpointer is not None
        await store.checkpointer.delete(run_id)
        await store.clear_active_run_id("s1")
        detail = (await ac.get("/api/sessions/s1")).json()
        assert detail["waiting"] is None
        assert (await _record(ac, "s1"))["status"] == "cancelled"
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "again", "session_id": "s1"}
        )
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def _start_task(app: Any, agent: Agent[Any]) -> Any:
    """Start ``agent`` in task chat ``t1`` (parent ``p1``) the way run_child
    does: an unregistered clone, a mailbox of its own, autostarted."""
    store = app.state.deps.store
    await store.upsert("p1", agent="bot", title="parent")
    await store.upsert("t1", agent="bot", title="task", parent_id="p1")
    return await app.state.deps.supervisor.start(
        session_id="t1",
        agent=agent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
        source="subagent:t1",
        mailbox=Mailbox(),
    )


@pytest.mark.asyncio
async def test_a_task_suspends_and_its_follower_waits_across_the_resume() -> None:
    # The task's agent is an unregistered clone: the resume uses the one this
    # process kept, and run_child (follow) sees the run end once answered.
    runs: list[str] = []
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("found it")])
    task_agent = _agent(provider, runs).clone(name="bot-sub")
    app = _app(_agent(provider, runs), suspend_after=0.2)
    deps = app.state.deps
    async with _client(app) as ac:
        ctrl = await _start_task(app, task_agent)
        follow = asyncio.create_task(deps.supervisor.follow("t1", ctrl))
        await _wait_run(ac, "t1", status="waiting")
        assert not follow.done()  # still the task's run: it only waits
        res = await ac.post(
            "/api/chat/approve",
            json={"session_id": "t1", "call_id": "c1", "decision": "approve"},
        )
        assert res.json()["resumed"] is True
        final = await asyncio.wait_for(follow, timeout=5)
    assert final is not None and final is not ctrl
    assert final.final_status == "completed" and final.final_output == "found it"
    assert final.source == "subagent:t1"
    assert runs == ["push"]


@pytest.mark.asyncio
async def test_a_stopped_waiting_task_ends_its_follower() -> None:
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    app = _app(_agent(provider, []), suspend_after=0.2)
    deps = app.state.deps
    async with _client(app) as ac:
        ctrl = await _start_task(app, _agent(provider, []).clone(name="bot-sub"))
        follow = asyncio.create_task(deps.supervisor.follow("t1", ctrl))
        await _wait_run(ac, "t1", status="waiting")
        await ac.post("/api/chat/cancel", params={"session_id": "t1"})
        assert await asyncio.wait_for(follow, timeout=5) is None
        assert deps.supervisor.kept("t1") is None


@pytest.mark.asyncio
async def test_shutdown_suspends_a_parked_run_and_stops_a_task() -> None:
    provider = ScriptedProvider(
        [call("push", {}, call_id="c1"), call("push", {}, call_id="t-c1")]
    )
    store = ChatStore.in_memory()
    app = _app(_agent(provider, []), store=store)  # no deadline: waits live
    deps = app.state.deps
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", status="blocked_on_approval")
        await _start_task(app, _agent(provider, []).clone(name="bot-sub"))
        await _wait_run(ac, "t1", status="blocked_on_approval")
        await deps.supervisor.shutdown()
        await asyncio.wait_for(task, timeout=5)
        # The chat's card outlives the restart; the task does not.
        detail = (await ac.get("/api/sessions/s1")).json()
        assert detail["waiting"]["id"] == "c1"
        assert await store.waiting_run("t1") is None


@pytest.mark.asyncio
async def test_startup_settles_a_waiting_task() -> None:
    # A task's agent and the run awaiting it died with the old process.
    provider = ScriptedProvider([call("push", {}, call_id="c1")])
    store = ChatStore.in_memory()
    app = _app(_agent(provider, []), suspend_after=0.2, store=store)
    async with _client(app) as ac:
        await _start_task(app, _agent(provider, []).clone(name="bot-sub"))
        await _wait_run(ac, "t1", status="waiting")
    await store.sweep_stale_runs()
    assert await store.waiting_run("t1") is None
    (record,) = await store.list_runs(session_id="t1")
    assert record.status == "interrupted"


@pytest.mark.asyncio
async def test_a_fresh_chat_schedule_skips_while_its_last_fire_waits() -> None:
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    store = ChatStore.in_memory()
    app = _app(_agent(provider, []), suspend_after=0.2, store=store)
    sched = Scheduler(app.state.deps)
    now = time.time()
    await store.add_schedule(_row(id="s", next_fire=now - 1, now=now))
    async with _client(app) as ac:
        await sched.run_due()
        first = await store.get_schedule("s")
        assert first is not None and first.last_session_id is not None
        await _wait_run(ac, first.last_session_id, status="waiting")
        await store.set_schedule_active("s", active=True, next_fire=time.time() - 1)
        await sched.run_due()
    skipped = [
        r for r in await store.list_runs(source="schedule:s") if r.status == "skipped"
    ]
    assert len(skipped) == 1 and "waiting for approval" in (skipped[0].error or "")
    again = await store.get_schedule("s")
    assert again is not None and again.last_session_id == first.last_session_id


@pytest.mark.asyncio
async def test_a_mode_switch_resumes_a_waiting_run_to_be_judged_again() -> None:
    mode = {"push": "ask"}
    runs: list[str] = []

    @tool(permission=lambda args, ctx: mode["push"])
    async def push() -> str:
        """Push to the remote."""
        runs.append("push")
        return "pushed"

    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    app = _app(Agent(name="bot", model=provider, tools=[push]), suspend_after=0.2)
    async with _client(app) as ac:
        await _suspend(ac)
        mode["push"] = "allow"  # what the new mode would say about the call
        await app.state.deps.switch_mode("s1", None)
        await _wait_run(ac, "s1", gone=True)
        assert (await _record(ac, "s1"))["status"] == "completed"
    assert runs == ["push"]  # allowed now: ran without being asked


@pytest.mark.asyncio
async def test_a_message_injected_while_the_run_suspends_waits_with_it() -> None:
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    store = ChatStore.in_memory()
    app = _app(_agent(provider, []), suspend_after=0.2, store=store)
    deps = app.state.deps
    finish = store.finish_run

    async def finish_run(run_id: str, **kw: Any) -> None:
        if kw.get("status") == "waiting":
            # A message arrives for the run while its record is being written.
            live = deps.supervisor.get("s1")
            assert live is not None
            live.inject("one more thing")
        await finish(run_id, **kw)

    store.finish_run = finish_run  # type: ignore[method-assign]
    async with _client(app) as ac:
        await _suspend(ac)
    record = await store.waiting_run("s1")
    assert record is not None and record.waiting_for is not None
    queued = [e["content"] for e in record.waiting_for["queued"]]
    assert queued == ["one more thing"]


def _child_spec(agent: Agent[Any], token: CancelToken, mailbox: Mailbox) -> ChildSpec:
    return ChildSpec(
        id="t1",
        agent=agent,
        prompt="go",
        token=token,
        parent_session_id="p1",
        max_turns=5,
        budget=None,
        mailbox=mailbox,
    )


async def _task_session(deps: Any) -> str:
    """The task chat run_child created under parent ``p1``."""
    for _ in range(250):
        if children := await deps.store.list_children("p1"):
            return str(children[0].id)
        await asyncio.sleep(0.02)
    raise AssertionError("no task chat")


@pytest.mark.asyncio
async def test_a_parent_cancel_stops_a_waiting_task() -> None:
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    app = _app(_agent(provider, []), suspend_after=0.2)
    deps = app.state.deps
    token = CancelToken()
    async with _client(app) as ac:
        await deps.store.upsert("p1", agent="bot", title="parent")
        child = _agent(provider, []).clone(name="bot-sub")
        run = asyncio.create_task(
            subagent_runner(deps)(_child_spec(child, token, Mailbox()))
        )
        sid = await _task_session(deps)
        await _wait_run(ac, sid, status="waiting")
        token.cancel("cancelled by the parent agent")
        with pytest.raises(RunCancelled):
            await asyncio.wait_for(run, timeout=5)
        assert await deps.store.waiting_run(sid) is None


@pytest.mark.asyncio
async def test_a_message_sent_to_a_waiting_task_is_read_after_the_resume() -> None:
    runs: list[str] = []
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("found it")])
    app = _app(_agent(provider, runs), suspend_after=0.2)
    deps = app.state.deps
    mailbox = Mailbox()
    async with _client(app) as ac:
        await deps.store.upsert("p1", agent="bot", title="parent")
        child = _agent(provider, runs).clone(name="bot-sub")
        run = asyncio.create_task(
            subagent_runner(deps)(_child_spec(child, CancelToken(), mailbox))
        )
        sid = await _task_session(deps)
        await _wait_run(ac, sid, status="waiting")
        mailbox.push("check the tags too")  # the parent's send_to_subagent
        await ac.post(
            "/api/chat/approve",
            json={"session_id": sid, "call_id": "c1", "decision": "approve"},
        )
        result = await asyncio.wait_for(run, timeout=5)
    assert result.output == "found it" and runs == ["push"]
    assert "check the tags too" in _texts(provider)


# ---- ask_human questions suspend too --------------------------------------


def _asking(provider: ScriptedProvider, channel: HumanChannel) -> Agent[Any]:
    return Agent(name="bot", model=provider, tools=[ask_human(channel)])


async def _ask(ac: Any, sid: str = "s1") -> list[tuple[str, Any]]:
    task, lines = _spawn(
        ac, "/api/chat/stream", json={"message": "go", "session_id": sid}
    )
    await asyncio.wait_for(task, timeout=5)
    await _wait_run(ac, sid, status="waiting")
    return _parse_sse("\n".join(lines))


@pytest.mark.asyncio
async def test_an_unanswered_question_suspends_and_its_answer_resumes() -> None:
    channel = HumanChannel()
    provider = ScriptedProvider(
        [call("ask_human", {"question": "Which branch?"}, call_id="q1"), text("ok")]
    )
    app = _app(_asking(provider, channel), question_channel=channel, suspend_after=0.2)
    async with app.router.lifespan_context(app), _client(app) as ac:
        evs = await _ask(ac)
        kinds = [e for e, _ in evs]
        assert dict(evs)["suspended"] == {"id": "q1"}
        assert "done" not in kinds and "error" not in kinds
        (row,) = (await ac.get("/api/runs")).json()
        assert row["status"] == "waiting" and row["pending_approval"] is None
        waiting = (await ac.get("/api/sessions/s1")).json()["waiting"]
        assert waiting["kind"] == "question" and waiting["id"] == "q1"
        assert waiting["question"] == "Which branch?"

        res = await ac.post(
            "/api/chat/answer", json={"session_id": "s1", "answer": "main"}
        )
        assert res.json() == {"ok": True, "resumed": True}
        await _wait_run(ac, "s1", gone=True)
        again = await ac.post(
            "/api/chat/answer", json={"session_id": "s1", "answer": "dev"}
        )
        assert again.status_code == 404
    assert "main" in _texts(provider)  # the question's result the model read


@pytest.mark.asyncio
async def test_shutdown_suspends_a_parked_question() -> None:
    channel = HumanChannel()
    provider = ScriptedProvider(
        [call("ask_human", {"question": "Which branch?"}, call_id="q1"), text("ok")]
    )
    store = ChatStore.in_memory()
    app = _app(_asking(provider, channel), question_channel=channel, store=store)
    async with _client(app) as ac:
        async with app.router.lifespan_context(app):
            task, _ = _spawn(
                ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
            )
            for _ in range(250):
                if app.state.deps.questions.pending("s1") is not None:
                    break
                await asyncio.sleep(0.02)
        await asyncio.wait_for(task, timeout=5)
        waiting = await store.waiting_run("s1")
        assert waiting is not None and waiting.waiting_for is not None
        assert waiting.waiting_for["kind"] == "question"


@pytest.mark.asyncio
async def test_another_tools_wait_resumes_on_a_message_not_an_answer() -> None:
    ready = {"ci": False}

    @tool
    async def wait_for_ci(question: str = "") -> str:
        """Wait for the CI (its argument only looks like a question)."""
        if not ready["ci"]:
            raise RunSuspended("the CI is still running")
        return "green"

    channel = HumanChannel()
    provider = ScriptedProvider(
        [call("wait_for_ci", {"question": "ok?"}, call_id="c1"), text("done")]
    )
    agent = Agent(name="bot", model=provider, tools=[wait_for_ci, ask_human(channel)])
    app = _app(agent, question_channel=channel)
    async with app.router.lifespan_context(app), _client(app) as ac:
        await _ask(ac)
        waiting = (await ac.get("/api/sessions/s1")).json()["waiting"]
        assert waiting["kind"] == "call" and "question" not in waiting
        # Not a question: the answer endpoint has nothing to answer.
        res = await ac.post(
            "/api/chat/answer", json={"session_id": "s1", "answer": "x"}
        )
        assert res.status_code == 404
        ready["ci"] = True
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "check again", "session_id": "s1"}
        )
        await asyncio.wait_for(task, timeout=5)
        await _wait_run(ac, "s1", gone=True)
        assert (await _record(ac, "s1"))["status"] == "completed"
