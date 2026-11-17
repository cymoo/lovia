"""Runs suspended on an approval nobody answered in time (``suspend_after``).

The run stops at the call and frees its slot; the card stays, and answering
it — from any tab, after any delay, from another app on the same store —
resumes the run with that answer. A message joins it, a stop settles it, a
report waits for it, and nothing starts a fresh run over it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("fastapi")

from lovia import Agent, tool  # noqa: E402
from lovia.web import subagent_deliver  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
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
        assert kinds.index("approval_required") < kinds.index("approval_suspended")
        assert "done" not in kinds and "error" not in kinds
        assert dict(evs)["approval_suspended"] == {"id": "c1"}

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
async def test_a_task_card_is_denied_rather_than_suspended() -> None:
    # A task's run is awaited by the run that spawned it: it must end.
    runs: list[str] = []
    provider = ScriptedProvider([call("push", {}, call_id="c1"), text("ack")])
    agent = _agent(provider, runs)
    store = ChatStore.in_memory()
    app = _app(agent, suspend_after=0.2, store=store)
    deps = app.state.deps
    async with _client(app) as ac:
        await store.upsert("t1", agent="bot", title="task")
        ctrl = await deps.supervisor.start(
            session_id="t1",
            agent=agent,
            input="go",
            is_new=False,
            title_message=None,
            autostart=True,
            source="subagent:t1",
        )
        assert ctrl.task is not None
        await asyncio.wait_for(ctrl.task, timeout=5)
        assert ctrl.final_status == "completed"
        assert (await ac.get("/api/runs")).json() == []
    assert runs == []
    assert any("did not answer within" in t for t in _texts(provider))


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
