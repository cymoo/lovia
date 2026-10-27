"""One run, one id (#308): the id a stream's ``session`` frame names is the
one ``GET /api/runs``, the lifecycle events and the run's history record
carry — across auto-chained legs, with no checkpointer, and through a resume
of a later leg. Plus ``busy="reject"``: a message never injected into a run."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from lovia import Agent, Mailbox  # noqa: E402
from lovia.checkpointer import RunHead  # noqa: E402
from lovia.messages import Usage  # noqa: E402
from lovia.stores import InMemorySession  # noqa: E402
from lovia.transcript import FinishDelta, InputEntry, TextDelta, UsageDelta  # noqa: E402
from lovia.web.store import ChatStore, RunRow  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_supervisor import _blocking_tool, _client, _spawn, _wait_run  # noqa: E402
from .test_web import _app, _parse_sse  # noqa: E402


def _record_events(app) -> list[tuple[str, dict]]:
    deps, seen = app.state.deps, []
    emit = deps.emit

    def recording(event, data):
        seen.append((event, dict(data)))
        emit(event, data)

    deps.emit = recording
    return seen


async def _post(ac, path: str, **kw):
    # A whole stream, bounded: a run that never ends fails the test, not hangs it.
    return await asyncio.wait_for(ac.post(path, **kw), timeout=10)


def _session_frame(body: str) -> dict:
    return next(d for e, d in _parse_sse(body) if e == "session")


def _run_ids(events, kind: str) -> list[str]:
    return [d["run_id"] for e, d in events if e == kind]


async def test_one_id_across_auto_chained_legs(monkeypatch) -> None:
    from lovia.web import supervisor as sup_mod

    # A push during the first model call lands after its turn-start drain, so
    # it is left over and the run auto-chains into a second leg.
    mailbox = Mailbox()
    monkeypatch.setattr(sup_mod, "Mailbox", lambda: mailbox)
    calls = 0

    class LatePush:
        name = "bot"
        supports_json_schema = False

        async def stream(
            self, entries, *, tools=None, response_format=None, settings=None
        ):
            nonlocal calls
            calls += 1
            if calls == 1:
                mailbox.push("more")
            yield TextDelta(text=f"leg {calls}")
            yield UsageDelta(usage=Usage(input_tokens=1, output_tokens=1))
            yield FinishDelta(reason="stop")

    store = ChatStore.in_memory()
    pointers: list[tuple[str, str | None]] = []
    orig_set = store.set_active_run_id

    async def spy(sid, run_id, *, context=None, record_id=None):
        pointers.append((run_id, record_id))
        await orig_set(sid, run_id, context=context, record_id=record_id)

    monkeypatch.setattr(store, "set_active_run_id", spy)
    app = _app(Agent(name="bot", model=LatePush()), store=store)
    events = _record_events(app)
    async with _client(app) as ac:
        res = await _post(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        history = (
            await ac.get("/api/runs/history", params={"session_id": "s1"})
        ).json()

    run_id = _session_frame(res.text)["run_id"]
    assert calls == 2  # two legs ran
    # Each leg checkpoints under its own id; the second names the run's.
    (first, _), (second, record) = pointers
    assert first == run_id and second != run_id and record == run_id
    assert _run_ids(events, "run_started") == [run_id]
    assert _run_ids(events, "run_finished") == [run_id]
    assert [(r["run_id"], r["status"]) for r in history] == [(run_id, "completed")]


async def test_one_id_without_a_checkpointer(tmp_path: Path) -> None:
    release = asyncio.Event()
    store = ChatStore(InMemorySession(), meta_path=tmp_path / "meta.db")
    assert store.checkpointer is None
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent, store=store)
    async with _client(app) as ac:
        task, lines = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        live = await _wait_run(ac, "s1")
        release.set()
        await asyncio.wait_for(task, timeout=5)
        one = (
            await ac.get("/api/runs/history", params={"run_id": live["run_id"]})
        ).json()

    assert live["run_id"]  # never null now
    assert _session_frame("\n".join(lines))["run_id"] == live["run_id"]
    assert [(r["run_id"], r["status"]) for r in one] == [(live["run_id"], "completed")]


async def _interrupted_leg(store: ChatStore, *, record_id: str | None) -> None:
    """A chat whose run was interrupted in a later leg, ``leg-2``, as if the
    process restarted; ``record_id`` is what the pointer names beside it."""
    await store.upsert("s1", agent="bot")
    now = time.time()
    await store.start_run(
        RunRow(
            id="run-R",
            session_id="s1",
            agent="bot",
            source="user",
            status="interrupted",
            error=None,
            started_at=now - 10,
            finished_at=now - 5,
            usage=None,
        )
    )
    await store.checkpointer.append(
        "leg-2",
        [InputEntry(role="user", content="more")],
        RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
    )
    await store.set_active_run_id("s1", "leg-2", record_id=record_id)


async def test_a_resumed_later_leg_finishes_its_runs_record() -> None:
    store = ChatStore.in_memory()
    await _interrupted_leg(store, record_id="run-R")
    app = _app(
        Agent(name="bot", model=ScriptedProvider([text("resumed")])), store=store
    )
    events = _record_events(app)
    async with _client(app) as ac:
        res = await _post(ac, "/api/chat/reconnect", params={"session_id": "s1"})
        history = (
            await ac.get("/api/runs/history", params={"session_id": "s1"})
        ).json()

    assert _session_frame(res.text)["run_id"] == "run-R"
    assert _run_ids(events, "run_finished") == ["run-R"]
    # The same record, finished — not a second one for the leg.
    assert [(r["run_id"], r["status"]) for r in history] == [("run-R", "completed")]


async def test_a_pointer_from_before_record_ids_resumes_as_its_checkpoint() -> None:
    store = ChatStore.in_memory()
    await _interrupted_leg(store, record_id=None)
    app = _app(
        Agent(name="bot", model=ScriptedProvider([text("resumed")])), store=store
    )
    async with _client(app) as ac:
        res = await _post(ac, "/api/chat/reconnect", params={"session_id": "s1"})
    assert _session_frame(res.text)["run_id"] == "leg-2"


# --------------------------------------------------------------------------- #
# busy="reject"
# --------------------------------------------------------------------------- #


async def test_reject_refuses_a_busy_chat_before_anything_runs() -> None:
    release = asyncio.Event()
    hooked: list[str] = []
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent, run_deps=lambda trigger: hooked.append(trigger.session_id))
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        res = await _post(
            ac,
            "/api/chat/stream",
            json={"message": "after", "session_id": "s1", "busy": "reject"},
        )
        release.set()
        await asyncio.wait_for(task, timeout=5)
        detail = (await ac.get("/api/sessions/s1")).json()

    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "run_active"
    assert hooked == ["s1"]  # only the running turn's
    assert [e["content"] for e in detail["entries"] if e["role"] == "user"] == ["go"]


async def test_reject_never_injects_into_a_run_that_won_the_race() -> None:
    """The chat is idle when the message arrives; while its run_deps hook
    runs, another request starts a run. The message is refused, not steered."""
    release, gate, in_hook = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def run_deps(trigger):
        if trigger.client_context.get("gate"):
            in_hook.set()
            await gate.wait()

    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent, run_deps=run_deps)
    async with _client(app) as ac:
        follow_up = asyncio.create_task(
            ac.post(
                "/api/chat/stream",
                json={
                    "message": "after",
                    "session_id": "s1",
                    "busy": "reject",
                    "context": {"gate": True},
                },
            )
        )
        await asyncio.wait_for(in_hook.wait(), timeout=5)
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        gate.set()
        res = await asyncio.wait_for(follow_up, timeout=5)
        release.set()
        await asyncio.wait_for(task, timeout=5)
        detail = (await ac.get("/api/sessions/s1")).json()
        history = (
            await ac.get("/api/runs/history", params={"session_id": "s1"})
        ).json()

    assert res.status_code == 409
    assert [e["content"] for e in detail["entries"] if e["role"] == "user"] == ["go"]
    assert len(history) == 1  # and no run of its own


async def test_inject_stays_the_default() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        live = await _wait_run(ac, "s1")
        steer, lines = _spawn(
            ac, "/api/chat/stream", json={"message": "steer", "session_id": "s1"}
        )
        await asyncio.sleep(0.1)
        release.set()
        await asyncio.wait_for(task, timeout=5)
        await asyncio.wait_for(steer, timeout=5)
        detail = (await ac.get("/api/sessions/s1")).json()

    # The joining stream names the run it joined.
    assert _session_frame("\n".join(lines))["run_id"] == live["run_id"]
    assert [e["content"] for e in detail["entries"] if e["role"] == "user"] == [
        "go",
        "steer",
    ]
