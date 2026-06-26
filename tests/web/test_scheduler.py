"""Phase 3 — scheduled / deferred background runs.

Three layers: trigger math (pure), the ``schedules`` store (SQLite CRUD + due
query), and the :class:`Scheduler` loop driven deterministically via the public
``run_due()`` (no wall-clock sleeps). The ``/api/schedules`` CRUD endpoints are
exercised with a sync ``TestClient`` (they only persist rows — no lifespan).
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent, tool  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.scheduler import (  # noqa: E402
    Scheduler,
    advance_next_fire,
    initial_next_fire,
    validate_trigger,
)
from lovia.web.store import ChatStore, ScheduleRow  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _agent(script, *, name="bot", tools=None):
    return Agent(name=name, model=ScriptedProvider(script), tools=tools or [])


def _app(agent, store):
    return create_app(agent, store=store, generate_titles=False)


def _row(**kw) -> ScheduleRow:
    """A ScheduleRow with sensible defaults; override any field via kwargs."""
    now = kw.pop("now", time.time())
    fields = {
        "id": "s1",
        "agent": "bot",
        "input": "do it",
        "session_id": None,
        "trigger_kind": "every",
        "trigger_expr": "3600",
        "next_fire": now,
        "active": True,
        "last_session_id": None,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(kw)
    return ScheduleRow(**fields)


async def _drain_runs(deps, timeout: float = 5.0) -> None:
    """Wait until every supervised run has finished and self-evicted."""
    deadline = time.time() + timeout
    while deps.supervisor._controllers:
        if time.time() > deadline:  # pragma: no cover - failure path
            raise AssertionError("background runs did not finish in time")
        await asyncio.sleep(0.02)


async def _wait_alive(deps, sid: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while deps.supervisor.get(sid) is None:
        if time.time() > deadline:  # pragma: no cover - failure path
            raise AssertionError(f"run {sid!r} never went live")
        await asyncio.sleep(0.02)


def _blocking_tool(release: asyncio.Event):
    @tool
    async def block() -> str:
        """Block until the test releases it."""
        await release.wait()
        return "ok"

    return block


# --------------------------------------------------------------------------- #
# trigger math
# --------------------------------------------------------------------------- #


def test_validate_trigger_accepts_valid() -> None:
    validate_trigger("every", "60")
    validate_trigger("every", "0.5")
    validate_trigger("at", "1700000000")


@pytest.mark.parametrize(
    ("kind", "expr"),
    [
        ("every", "0"),  # must be > 0
        ("every", "-5"),
        ("every", "nope"),
        ("at", "notanumber"),
        ("bogus", "x"),  # unknown kind
    ],
)
def test_validate_trigger_rejects_bad(kind: str, expr: str) -> None:
    with pytest.raises(ValueError):
        validate_trigger(kind, expr)


def test_validate_trigger_rejects_bad_cron() -> None:
    pytest.importorskip("croniter")
    with pytest.raises(ValueError):
        validate_trigger("cron", "not a cron expression")


def test_next_fire_every_and_at() -> None:
    now = 1000.0
    assert initial_next_fire("every", "60", now=now) == 1060.0
    assert advance_next_fire("every", "60", now=now) == 1060.0
    # 'at' is one-shot: first fire is the timestamp, then it deactivates.
    assert initial_next_fire("at", "1500", now=now) == 1500.0
    assert advance_next_fire("at", "1500", now=now) is None


def test_next_fire_cron_is_wired() -> None:
    pytest.importorskip("croniter")
    now = 1_700_000_000.0
    n1 = initial_next_fire("cron", "*/5 * * * *", now=now)
    assert 0 < n1 - now <= 300  # next 5-minute slot, in the future
    n2 = advance_next_fire("cron", "*/5 * * * *", now=n1)
    assert n2 - n1 == 300  # successive slots are one interval apart


# --------------------------------------------------------------------------- #
# ScheduleStore
# --------------------------------------------------------------------------- #


async def test_schedule_store_crud() -> None:
    store = ChatStore.in_memory()
    now = 1000.0
    await store.add_schedule(_row(id="a", now=now, next_fire=now + 10))
    await store.add_schedule(_row(id="b", now=now, next_fire=now - 10))

    got = await store.get_schedule("a")
    assert got is not None and got.id == "a" and got.active
    assert {r.id for r in await store.list_schedules()} == {"a", "b"}

    # Only 'b' is due (next_fire <= now); 'a' is in the future.
    assert [r.id for r in await store.due_schedules(now)] == ["b"]

    assert await store.delete_schedule("a") is True
    assert await store.delete_schedule("a") is False
    assert await store.get_schedule("a") is None


async def test_schedule_store_mark_fired_and_pause_resume() -> None:
    store = ChatStore.in_memory()
    now = 1000.0
    await store.add_schedule(_row(id="a", now=now, next_fire=now))

    await store.mark_fired(
        "a", next_fire=now + 60, active=True, last_session_id="sess1"
    )
    r = await store.get_schedule("a")
    assert r is not None
    assert r.next_fire == now + 60 and r.active and r.last_session_id == "sess1"

    # Pause: inactive rows never come back as due, even once their slot passes.
    await store.set_schedule_active("a", active=False)
    paused = await store.get_schedule("a")
    assert paused is not None and not paused.active
    assert "a" not in [r.id for r in await store.due_schedules(now + 10_000)]

    # Resume with a freshly-computed next_fire.
    await store.set_schedule_active("a", active=True, next_fire=now + 5)
    resumed = await store.get_schedule("a")
    assert resumed is not None and resumed.active and resumed.next_fire == now + 5


# --------------------------------------------------------------------------- #
# Scheduler loop
# --------------------------------------------------------------------------- #


async def test_scheduler_fires_a_fresh_session() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("hi from cron")]), store)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(
        _row(id="s", input="run me", trigger_expr="3600", next_fire=now - 1, now=now)
    )

    await Scheduler(deps).run_due()
    await _drain_runs(deps)

    after = await store.get_schedule("s")
    assert after is not None
    assert after.active  # 'every' stays active
    assert after.last_session_id is not None
    assert after.next_fire > now  # advanced into the future

    # The headless run created the session and produced the scripted reply.
    entries = await store.session.load(after.last_session_id)
    contents = [getattr(e, "content", None) for e in entries]
    assert "run me" in contents and "hi from cron" in contents
    meta = await store.get(after.last_session_id)
    assert meta is not None and meta.agent == "bot"


async def test_scheduler_one_shot_at_deactivates() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("once")]), store)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(
        _row(id="s", trigger_kind="at", trigger_expr=str(now - 1), next_fire=now - 1)
    )

    await Scheduler(deps).run_due()
    await _drain_runs(deps)

    after = await store.get_schedule("s")
    assert after is not None
    assert not after.active  # one-shot fired → deactivated
    assert after.last_session_id is not None


async def test_scheduler_coalesces_overdue_slots() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("tick")]), store)
    deps = app.state.deps
    now = time.time()
    # next_fire far in the past with a 60s interval = thousands of missed slots.
    await store.add_schedule(
        _row(id="s", trigger_expr="60", next_fire=now - 10_000, now=now)
    )

    await Scheduler(deps).run_due()
    await _drain_runs(deps)

    after = await store.get_schedule("s")
    assert after is not None
    # Fired once; next_fire jumped to ~now+60 (computed from now, not the stale
    # slot) — the missed slots collapsed instead of replaying.
    assert now < after.next_fire <= now + 61


async def test_scheduler_skips_when_previous_run_active() -> None:
    release = asyncio.Event()
    block = _blocking_tool(release)
    store = ChatStore.in_memory()
    app = _app(
        _agent([call("block", {}, call_id="c1"), text("done")], tools=[block]), store
    )
    deps = app.state.deps
    sched = Scheduler(deps)
    now = time.time()
    await store.add_schedule(_row(id="s", next_fire=now - 1, now=now))

    try:
        await sched.run_due()  # fires run1, which blocks on the tool
        first = await store.get_schedule("s")
        assert first is not None and first.last_session_id is not None
        s1 = first.last_session_id
        await _wait_alive(deps, s1)

        # Force it due again while run1 is still live → must skip (no pile-up).
        await store.set_schedule_active("s", active=True, next_fire=time.time() - 1)
        await sched.run_due()

        assert list(deps.supervisor._controllers) == [s1]  # no second run
        again = await store.get_schedule("s")
        assert again is not None and again.last_session_id == s1
    finally:
        release.set()
        await _drain_runs(deps)


async def test_scheduler_injects_into_live_session() -> None:
    release = asyncio.Event()
    block = _blocking_tool(release)
    store = ChatStore.in_memory()
    agent = _agent([call("block", {}, call_id="c1"), text("done")], tools=[block])
    app = _app(agent, store)
    deps = app.state.deps
    now = time.time()

    try:
        ctrl = await deps.supervisor.start(
            session_id="conv",
            agent=agent,
            input="orig",
            is_new=True,
            title_message="orig",
            autostart=True,
        )
        await _wait_alive(deps, "conv")

        # A schedule targeting that live session injects rather than spawning.
        await store.add_schedule(
            _row(id="s", session_id="conv", next_fire=now - 1, now=now)
        )
        await Scheduler(deps).run_due()

        assert list(deps.supervisor._controllers) == ["conv"]  # no new session
        assert ctrl.mailbox.drain() == ["do it"]  # the scheduled input was injected
        after = await store.get_schedule("s")
        assert after is not None
        assert after.last_session_id == "conv" and after.next_fire > now
    finally:
        release.set()
        await _drain_runs(deps)


async def test_scheduler_advances_past_unavailable_agent() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("hi")]), store)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(
        _row(id="s", agent="ghost", trigger_expr="60", next_fire=now - 1, now=now)
    )

    await Scheduler(deps).run_due()

    after = await store.get_schedule("s")
    assert after is not None
    assert after.next_fire > now  # advanced so it won't hot-loop
    assert not deps.supervisor._controllers  # nothing fired


def test_scheduler_lifespan_fires_end_to_end() -> None:
    """The real loop (started by create_app's lifespan) fires a due schedule."""
    app = create_app(
        _agent([text("fired")]),
        store=ChatStore.in_memory(),
        generate_titles=False,
        scheduler_poll=0.05,  # tick fast so the test doesn't dawdle
    )
    with TestClient(app) as c:  # entering the context runs the lifespan
        # An 'at' schedule in the past is due on the very next tick.
        r = c.post(
            "/api/schedules",
            json={
                "input": "ping",
                "trigger_kind": "at",
                "trigger_expr": str(time.time() - 1),
            },
        )
        assert r.status_code == 200, r.text
        sid = r.json()["id"]

        for _ in range(100):
            row = next(
                (s for s in c.get("/api/schedules").json() if s["id"] == sid), None
            )
            if row is not None and not row["active"]:  # one-shot fired → deactivated
                break
            time.sleep(0.05)
        else:  # pragma: no cover - failure path
            raise AssertionError("the live scheduler never fired the due schedule")

        # The fire created a session through the supervisor.
        assert len(c.get("/api/sessions").json()) >= 1


# --------------------------------------------------------------------------- #
# /api/schedules
# --------------------------------------------------------------------------- #


def test_info_advertises_scheduling() -> None:
    app = _app(_agent([text("hi")]), ChatStore.in_memory())
    info = TestClient(app).get("/api/info").json()
    assert info["features"]["scheduling"] is True


def test_api_schedule_crud() -> None:
    app = _app(_agent([text("hi")]), ChatStore.in_memory())
    c = TestClient(app)

    r = c.post(
        "/api/schedules",
        json={"input": "ping", "trigger_kind": "every", "trigger_expr": "300"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    sid = body["id"]
    assert body["trigger_kind"] == "every"
    assert body["active"] is True
    assert body["agent"] == "bot"
    assert body["next_fire"] > 0

    assert any(s["id"] == sid for s in c.get("/api/schedules").json())

    # Pause → resume (resume recomputes next_fire from now).
    paused = c.patch(f"/api/schedules/{sid}", json={"active": False})
    assert paused.status_code == 200 and paused.json()["active"] is False
    resumed = c.patch(f"/api/schedules/{sid}", json={"active": True})
    assert resumed.json()["active"] is True and resumed.json()["next_fire"] > 0

    assert c.delete(f"/api/schedules/{sid}").status_code == 200
    assert c.delete(f"/api/schedules/{sid}").status_code == 404
    assert c.patch(f"/api/schedules/{sid}", json={"active": False}).status_code == 404


@pytest.mark.parametrize("kind,expr", [("every", "300"), ("at", "1700000000")])
def test_api_create_each_trigger_kind(kind: str, expr: str) -> None:
    app = _app(_agent([text("hi")]), ChatStore.in_memory())
    r = TestClient(app).post(
        "/api/schedules",
        json={"input": "x", "trigger_kind": kind, "trigger_expr": expr},
    )
    assert r.status_code == 200, r.text
    assert r.json()["trigger_kind"] == kind


def test_api_create_cron() -> None:
    pytest.importorskip("croniter")
    app = _app(_agent([text("hi")]), ChatStore.in_memory())
    r = TestClient(app).post(
        "/api/schedules",
        json={"input": "x", "trigger_kind": "cron", "trigger_expr": "*/5 * * * *"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["next_fire"] > 0


def test_api_create_rejects_bad_input() -> None:
    app = _app(_agent([text("hi")]), ChatStore.in_memory())
    c = TestClient(app)
    # Bad trigger expression → 422.
    assert (
        c.post(
            "/api/schedules",
            json={"input": "x", "trigger_kind": "every", "trigger_expr": "0"},
        ).status_code
        == 422
    )
    # Unknown agent → 404.
    assert (
        c.post(
            "/api/schedules",
            json={
                "input": "x",
                "agent": "ghost",
                "trigger_kind": "every",
                "trigger_expr": "60",
            },
        ).status_code
        == 404
    )
    # Blank input → 422.
    assert (
        c.post(
            "/api/schedules",
            json={"input": "   ", "trigger_kind": "every", "trigger_expr": "60"},
        ).status_code
        == 422
    )
