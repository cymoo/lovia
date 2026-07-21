"""The /api/events lifecycle stream (process-wide event bus).

The bus is exercised at two levels: payloads published on ``deps.bus`` (what
any consumer sees), and the SSE endpoint end-to-end via httpx (what the
bundled UI's EventSource consumes). Runs come from the same supervised
machinery the rest of the web tests drive.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web.store import ChatStore, ScheduleRow  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_supervisor import (  # noqa: E402
    _blocking_tool,
    _client,
    _kill,
    _spawn,
    _wait_run,
)
from .test_web import _app, _parse_sse  # noqa: E402


def _spawn_get(ac, path):
    """Consume a GET SSE endpoint in a task; returns (task, lines)."""
    lines: list[str] = []

    async def run() -> None:
        async with ac.stream("GET", path) as res:
            async for line in res.aiter_lines():
                lines.append(line)

    return asyncio.create_task(run()), lines


def _drain_bus(sub) -> list[tuple[str, dict]]:
    """Synchronously drain everything queued on a bus subscription."""
    out: list[tuple[str, dict]] = []
    while True:
        try:
            _seq, payload = sub._q.get_nowait()
        except asyncio.QueueEmpty:
            return out
        if not isinstance(payload, dict):  # close sentinel
            return out
        out.append((payload["event"], json.loads(payload["data"])))


async def _collect(sub, *, until: set[str], timeout: float = 5.0):
    """Drain the bus until every event type in ``until`` has been seen.

    Terminal emits land in the run task's wind-down (after eviction), so a
    test can't sync on the HTTP response alone — hence the poll.
    """
    events: list[tuple[str, dict]] = []
    for _ in range(int(timeout / 0.02)):
        events.extend(_drain_bus(sub))
        if until <= {e for e, _d in events}:
            return events
        await asyncio.sleep(0.02)
    raise AssertionError(f"bus never delivered {until}; got {[e for e, _d in events]}")


def test_info_advertises_events() -> None:
    app = _app(Agent(name="bot", model=ScriptedProvider([text("hi")])))
    info = TestClient(app).get("/api/info").json()
    assert info["features"]["events"] is True


@pytest.mark.asyncio
async def test_bus_publishes_run_and_session_lifecycle() -> None:
    provider = ScriptedProvider([text("done")])
    agent = Agent(name="bot", model=provider)
    app = _app(agent)
    deps = app.state.deps
    sub = deps.bus.subscribe()
    try:
        async with _client(app) as ac:
            task, _ = _spawn(
                ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
            )
            await _wait_run(ac, "s1", gone=True)
            await task
        events = await _collect(sub, until={"run_finished"})
    finally:
        sub.close()

    assert [e for e, _d in events] == [
        "session_created",
        "run_started",
        "run_finished",
    ]
    by_kind = dict(events)
    assert by_kind["session_created"]["session_id"] == "s1"
    assert by_kind["session_created"]["agent"] == "bot"
    assert by_kind["run_started"]["source"] == "user"
    assert by_kind["run_finished"]["status"] == "completed"
    assert by_kind["run_finished"]["error"] is None
    assert by_kind["run_finished"]["run_id"] == by_kind["run_started"]["run_id"]


@pytest.mark.asyncio
async def test_bus_publishes_cancelled_outcome_and_retitle() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent)
    deps = app.state.deps
    sub = deps.bus.subscribe()
    try:
        async with _client(app) as ac:
            task, _ = _spawn(
                ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
            )
            await _wait_run(ac, "s1")
            await ac.post("/api/chat/cancel", params={"session_id": "s1"})
            release.set()
            await _kill(task)
            await ac.patch("/api/sessions/s1", json={"title": "Renamed"})
            events = await _collect(sub, until={"run_finished", "session_retitled"})
    finally:
        sub.close()

    finished = next(d for e, d in events if e == "run_finished")
    assert finished["status"] == "cancelled" and finished["error"] == "cancelled"
    retitled = next(d for e, d in events if e == "session_retitled")
    assert retitled == {"session_id": "s1", "title": "Renamed"}


@pytest.mark.asyncio
async def test_events_endpoint_streams_lifecycle_over_sse() -> None:
    provider = ScriptedProvider([text("done")])
    agent = Agent(name="bot", model=provider)
    app = _app(agent)
    deps = app.state.deps
    async with _client(app) as ac:
        es_task, lines = _spawn_get(ac, "/api/events")
        for _ in range(250):  # wait for the SSE subscription to attach
            if deps.bus._subs:
                break
            await asyncio.sleep(0.02)

        run_task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", gone=True)
        await run_task
        for _ in range(250):  # the terminal emit lands in the run's wind-down
            if deps.bus.seq >= 3:
                break
            await asyncio.sleep(0.02)
        # httpx's ASGITransport buffers the whole SSE body — end the stream so
        # the buffered events flush (in production the connection stays open).
        deps.bus.close()
        await asyncio.wait_for(es_task, timeout=5)

    evs = [
        (e, d)
        for e, d in _parse_sse("\n".join(lines))
        if isinstance(d, dict)  # drop pings/comments
    ]
    assert [e for e, _d in evs] == ["session_created", "run_started", "run_finished"]
    assert all(d["session_id"] == "s1" for _e, d in evs)
    assert evs[-1][1]["status"] == "completed"


@pytest.mark.asyncio
async def test_scheduler_fire_announces_created_session() -> None:
    from lovia.web.scheduler import Scheduler

    store = ChatStore.in_memory()
    app = _app(Agent(name="bot", model=ScriptedProvider([text("ok")])), store=store)
    deps = app.state.deps
    sub = deps.bus.subscribe()
    now = time.time()
    await store.add_schedule(
        ScheduleRow(
            id="s",
            agent="bot",
            input="go",
            session_id=None,
            trigger_kind="every",
            trigger_expr="3600",
            next_fire=now - 1,
            active=True,
            last_session_id=None,
            created_at=now,
            updated_at=now,
        )
    )
    try:
        await Scheduler(deps).run_due()
        events = await _collect(sub, until={"run_finished"})
    finally:
        sub.close()

    assert [e for e, _d in events] == [
        "session_created",
        "run_started",
        "run_finished",
    ]
    started = next(d for e, d in events if e == "run_started")
    assert started["source"] == "schedule:s"
    created = next(d for e, d in events if e == "session_created")
    assert created["agent"] == "bot" and created["title"]
