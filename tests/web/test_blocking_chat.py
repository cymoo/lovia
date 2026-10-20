"""The blocking ``POST /api/chat`` turn runs supervised, like a stream's.

The request starts a supervised run and waits it out, so the turn holds the
chat, answers to a stop, a deletion and shutdown, receives subagent reports
through its mailbox, and survives its client going away. Blocked turns are
posted in a task while the test drives the rest (see ``test_supervisor``).
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from lovia import Agent, Subagents, tool  # noqa: E402
from lovia.checkpointer import RunHead  # noqa: E402
from lovia.messages import Usage  # noqa: E402
from lovia.tools.human import HumanChannel, ask_human  # noqa: E402
from lovia.transcript import InputEntry  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_subagents import GatedProvider  # noqa: E402
from .test_supervisor import (  # noqa: E402
    _blocking_tool,
    _client,
    _kill,
    _ScriptThenFail,
    _spawn,
    _wait_calls,
    _wait_run,
)
from .test_web import _app, _parse_sse  # noqa: E402


def _post(ac, message: str, sid: str = "s1") -> asyncio.Task:
    return asyncio.create_task(
        ac.post("/api/chat", json={"message": message, "session_id": sid})
    )


def _user_texts(entries) -> list[str]:
    return [
        e.content
        for e in entries
        if isinstance(e, InputEntry) and isinstance(e.content, str)
    ]


async def test_blocking_turn_holds_the_chat() -> None:
    """A stream for the chat joins the blocking turn's run instead of starting
    a second one; another blocking turn is refused."""
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("both done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    store = ChatStore.in_memory()
    async with _client(_app(agent, store=store)) as ac:
        turn = _post(ac, "go")
        await _wait_run(ac, "s1")  # held by a supervised run
        again = await ac.post("/api/chat", json={"message": "hi", "session_id": "s1"})
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "run_active"

        stream, lines = _spawn(
            ac, "/api/chat/stream", json={"message": "also", "session_id": "s1"}
        )
        await asyncio.sleep(0.1)  # attached; its message queued in the mailbox
        release.set()
        res = await asyncio.wait_for(turn, timeout=5)
        await asyncio.wait_for(stream, timeout=5)
        history = (await ac.get("/api/runs/history?session_id=s1")).json()

    assert res.status_code == 200, res.text
    # The injected message reached the same run: its answer is the turn's.
    assert res.json()["output"] == "both done"
    assert len(provider.calls) == 2
    assert [e for e, _ in _parse_sse("\n".join(lines))][:2] == ["session", "snapshot"]
    assert len(history) == 1
    assert _user_texts(await store.session.load("s1")) == ["go", "also"]


async def test_cancel_stops_a_blocking_turn() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("never")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    store = ChatStore.in_memory()
    async with _client(_app(agent, store=store)) as ac:
        turn = _post(ac, "go")
        await _wait_run(ac, "s1")
        stop = await ac.post("/api/chat/cancel", params={"session_id": "s1"})
        assert stop.status_code == 200
        release.set()  # cooperative: the run stops once the tool returns
        res = await asyncio.wait_for(turn, timeout=5)

    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "run_cancelled"
    assert len(provider.calls) == 1  # no turn after the stop
    assert _user_texts(await store.session.load("s1")) == ["go"]  # partial kept


async def test_deleting_the_chat_stops_a_blocking_turn() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("never")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    store = ChatStore.in_memory()
    async with _client(_app(agent, store=store)) as ac:
        turn = _post(ac, "go")
        await _wait_run(ac, "s1")
        assert (await ac.delete("/api/sessions/s1")).status_code == 200
        release.set()
        res = await asyncio.wait_for(turn, timeout=5)

    assert res.json()["detail"]["code"] == "run_cancelled"
    assert len(provider.calls) == 1
    # The wind-down wrote nothing back into the deleted chat.
    assert await store.get("s1") is None
    assert await store.session.load("s1") == []


async def test_stop_frees_a_blocking_turn_parked_on_ask_human() -> None:
    channel = HumanChannel()
    provider = ScriptedProvider(
        [call("ask_human", {"question": "which one?"}, call_id="c1"), text("never")]
    )
    agent = Agent(name="bot", model=provider, tools=[ask_human(channel)])
    app = _app(agent, question_channel=channel)
    questions = app.state.deps.questions
    questions.start()  # the lifespan's job, which ASGITransport doesn't run
    try:
        async with _client(app) as ac:
            turn = _post(ac, "go")
            for _ in range(250):
                if questions.pending("s1") is not None:
                    break
                await asyncio.sleep(0.02)
            assert questions.pending("s1") is not None
            await ac.post("/api/chat/cancel", params={"session_id": "s1"})
            # Nobody answers: the stop alone must free the parked turn.
            res = await asyncio.wait_for(turn, timeout=5)
    finally:
        await questions.aclose()
    assert res.json()["detail"]["code"] == "run_cancelled"


async def test_blocking_turn_does_not_park_on_approval() -> None:
    """No client sees a blocking turn's approval request, so (as before
    supervision, and as for ``Runner.run``) the agent's approval_handler
    decides, else the call is denied — the run never waits."""
    ran: list[str] = []

    @tool(needs_approval=True)
    async def sensitive() -> str:
        """Sensitive."""
        ran.append("yes")
        return "did it"

    def script() -> ScriptedProvider:
        return ScriptedProvider([call("sensitive", {}, call_id="c1"), text("ack")])

    denied = Agent(name="bot", model=script(), tools=[sensitive])
    async with _client(_app(denied)) as ac:
        res = await asyncio.wait_for(
            ac.post("/api/chat", json={"message": "go"}), timeout=5
        )
    assert res.status_code == 200 and res.json()["output"] == "ack"
    assert ran == []

    allowed = denied.clone(model=script(), approval_handler=lambda call, ctx: True)
    async with _client(_app(allowed)) as ac:
        res = await asyncio.wait_for(
            ac.post("/api/chat", json={"message": "go"}), timeout=5
        )
    assert res.status_code == 200 and res.json()["output"] == "ack"
    assert ran == ["yes"]


async def test_shutdown_cancels_a_blocking_turn() -> None:
    never = asyncio.Event()  # the tool ignores the cooperative stop
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("never")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(never)])
    app = _app(agent)
    async with _client(app) as ac:
        turn = _post(ac, "go")
        await _wait_run(ac, "s1")
        await app.state.deps.supervisor.shutdown(grace=0.2)
        res = await asyncio.wait_for(turn, timeout=5)
    # Answered, not hung — and not a 500 from the hard-cancelled task.
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "run_cancelled"


async def test_blocking_turn_outlives_its_client() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    store = ChatStore.in_memory()
    async with _client(_app(agent, store=store)) as ac:
        turn = _post(ac, "go")
        await _wait_run(ac, "s1")
        await _kill(turn)  # the client goes away mid-turn
        await _wait_run(ac, "s1")  # ... and the run is still going
        release.set()
        await _wait_run(ac, "s1", gone=True)
    entries = await store.session.load("s1")
    assert _user_texts(entries) == ["go"]
    assert any(getattr(e, "content", None) == "done" for e in entries)


async def test_blocking_turn_failure_answers_run_failed() -> None:
    agent = Agent(name="bot", model=_ScriptThenFail([]))
    async with _client(_app(agent)) as ac:
        res = await ac.post("/api/chat", json={"message": "go"})
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert detail["code"] == "run_failed"
    assert "scripted non-retryable failure" in detail["message"]


async def test_refused_blocking_turn_keeps_the_interrupted_run() -> None:
    """The chat's interrupted run is deleted only once a new run has taken the
    chat: a turn refused at the run cap leaves it resumable."""
    store = ChatStore.in_memory()
    await store.upsert("s1", agent="bot")
    await store.checkpointer.append(
        "run-1",
        [InputEntry(role="user", content="q")],
        RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
    )
    await store.set_active_run_id("s1", "run-1")
    release = asyncio.Event()
    provider = ScriptedProvider(
        [call("block", {}, call_id="c1"), text("other done"), text("s1 done")]
    )
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    async with _client(_app(agent, store=store, max_background_runs=1)) as ac:
        other = _post(ac, "go", sid="s2")
        await _wait_run(ac, "s2")  # the one slot is taken
        refused = await ac.post("/api/chat", json={"message": "hi", "session_id": "s1"})
        assert refused.status_code == 429
        assert await store.checkpointer.load("run-1") is not None
        assert await store.get_active_run_id("s1") == "run-1"

        release.set()
        await asyncio.wait_for(other, timeout=5)
        res = await ac.post("/api/chat", json={"message": "hi", "session_id": "s1"})
    assert res.status_code == 200, res.text
    assert await store.checkpointer.load("run-1") is None  # superseded: dropped


async def test_failed_auto_chain_hop_is_not_a_success(monkeypatch) -> None:
    """A leg that completed doesn't make the turn a success when the run then
    fails to chain the next one (the stream gets an error event for it)."""
    gate = asyncio.Event()
    provider = ScriptedProvider([text("first"), text("second")])
    agent = Agent(name="bot", model=GatedProvider(provider, gate))
    store = ChatStore.in_memory()
    async with _client(_app(agent, store=store)) as ac:
        turn = _post(ac, "go")
        await _wait_run(ac, "s1")  # parked in the first leg's model call
        queued = await ac.post(
            "/api/chat/inject", json={"session_id": "s1", "message": "more"}
        )
        assert queued.json()["accepted"]  # left over: the run chains a leg

        async def db_down(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(store, "set_active_run_id", db_down)
        gate.set()
        res = await asyncio.wait_for(turn, timeout=5)
    assert res.status_code == 500
    assert res.json()["detail"] == {"code": "run_failed", "message": "db down"}


async def test_blocking_parent_receives_its_subagent_report() -> None:
    """The report reaches the waiting blocking turn through its mailbox,
    instead of a second report run on the same chat."""
    child = Agent(name="researcher", model=ScriptedProvider([text("findings")]))
    provider = ScriptedProvider(
        [
            call("spawn_subagent", {"prompt": "dig"}, call_id="c1"),
            call("wait_subagents", {}, call_id="c2"),
            text("summarised the findings"),
        ]
    )
    agent = Agent(name="bot", model=provider, plugins=[Subagents(child)])
    app = _app(agent)
    deps = app.state.deps
    async with _client(app) as ac:
        res = await asyncio.wait_for(
            ac.post("/api/chat", json={"message": "go", "session_id": "s1"}),
            timeout=5,
        )
    assert res.status_code == 200, res.text
    assert res.json()["output"] == "summarised the findings"
    await _wait_calls(provider, 3)
    assert "findings" in str(provider.calls[2])  # the report, in this run
    assert await deps.store.latest_run_for("subagent-report:t1") is None
    assert any("findings" in t for t in _user_texts(await deps.session.load("s1")))
