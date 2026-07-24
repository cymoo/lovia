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
from lovia.exceptions import ProviderError  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.scheduler import (  # noqa: E402
    Scheduler,
    advance_next_fire,
    fire_input,
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


def test_fire_input_plain_without_until() -> None:
    assert fire_input(_row(input="just do it")) == "just do it"


def test_fire_input_appends_protocol_with_until() -> None:
    out = fire_input(_row(id="abc123", input="check the log", until="it says ready"))
    assert out.startswith("check the log")
    # The protocol block names the schedule, the condition, and the tool.
    assert "abc123" in out
    assert "it says ready" in out
    assert "cancel_schedule" in out


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


async def test_schedule_store_stop_condition_fields() -> None:
    store = ChatStore.in_memory()
    now = 1000.0
    await store.add_schedule(
        _row(id="a", now=now, until="done", max_fires=3, expires_at=now + 60)
    )
    r = await store.get_schedule("a")
    assert r is not None
    assert r.until == "done" and r.max_fires == 3 and r.expires_at == now + 60
    assert r.fire_count == 0 and r.finished_reason is None

    # Each mark_fired counts one attempt; a tripped net writes the reason.
    await store.mark_fired("a", next_fire=now + 60, active=True, last_session_id="s1")
    r = await store.get_schedule("a")
    assert r is not None and r.fire_count == 1 and r.finished_reason is None
    await store.mark_fired(
        "a",
        next_fire=now + 120,
        active=False,
        last_session_id="s2",
        finished_reason="max fires reached",
    )
    r = await store.get_schedule("a")
    assert r is not None and r.fire_count == 2 and not r.active
    assert r.finished_reason == "max fires reached"

    # Resume clears the reason — the schedule is live again, not "done".
    await store.set_schedule_active("a", active=True, next_fire=now + 5)
    r = await store.get_schedule("a")
    assert r is not None and r.active and r.finished_reason is None

    # mark_fired races the run it launched: it must never resurrect a row the
    # run just cancelled, nor erase the cancel reason.
    await store.set_schedule_active("a", active=False, finished_reason="condition met")
    await store.mark_fired("a", next_fire=now + 180, active=True, last_session_id="s3")
    r = await store.get_schedule("a")
    assert r is not None and not r.active and r.finished_reason == "condition met"


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


async def test_scheduler_deactivates_expired_schedule_without_firing() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("never")]), store)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(
        _row(id="s", next_fire=now - 1, now=now, expires_at=now - 0.5)
    )

    await Scheduler(deps).run_due()

    assert not deps.supervisor._controllers  # nothing fired
    after = await store.get_schedule("s")
    assert after is not None
    assert not after.active
    assert after.finished_reason == "expired"
    assert after.fire_count == 0
    assert await store.latest_run_for("schedule:s") is None


async def test_scheduler_max_fires_deactivates_after_last_fire() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("one"), text("two")]), store)
    deps = app.state.deps
    sched = Scheduler(deps)
    now = time.time()
    await store.add_schedule(_row(id="s", next_fire=now - 1, now=now, max_fires=2))

    await sched.run_due()
    await _drain_runs(deps)
    mid = await store.get_schedule("s")
    assert mid is not None and mid.active and mid.fire_count == 1

    # Force the second (and per max_fires last) fire.
    await store.set_schedule_active("s", active=True, next_fire=time.time() - 1)
    await sched.run_due()
    await _drain_runs(deps)

    after = await store.get_schedule("s")
    assert after is not None
    assert not after.active  # the Nth fire still ran, then the net tripped
    assert after.fire_count == 2
    assert after.finished_reason == "max fires reached"

    # Inactive rows are never due — a further tick fires nothing.
    await sched.run_due()
    assert not deps.supervisor._controllers


async def test_fire_delivers_until_protocol_block() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("checked")]), store)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(
        _row(
            id="s",
            input="check the log",
            until="it says ready",
            max_fires=10,
            next_fire=now - 1,
            now=now,
        )
    )

    await Scheduler(deps).run_due()
    await _drain_runs(deps)

    after = await store.get_schedule("s")
    assert after is not None and after.last_session_id is not None
    entries = await store.session.load(after.last_session_id)
    contents = [getattr(e, "content", None) or "" for e in entries]
    proto = next(c for c in contents if "cancel_schedule" in c)
    assert proto.startswith("check the log")
    assert "schedule s" in proto and "it says ready" in proto
    # The protocol boilerplate stays out of the session title.
    meta = await store.get(after.last_session_id)
    assert meta is not None and meta.title is not None
    assert "cancel_schedule" not in meta.title


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


def test_api_create_requires_agent_when_ambiguous() -> None:
    agents = {
        "alpha": _agent([text("a")], name="alpha"),
        "beta": _agent([text("b")], name="beta"),
    }
    app = create_app(agents, store=ChatStore.in_memory(), generate_titles=False)
    # Omitting `agent` with >1 agent (no default) → 404 with a clear message,
    # not a confusing "unknown agent None".
    r = TestClient(app).post(
        "/api/schedules",
        json={"input": "x", "trigger_kind": "every", "trigger_expr": "60"},
    )
    assert r.status_code == 404
    assert "no agent specified" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# concurrency cap
# --------------------------------------------------------------------------- #


async def test_scheduler_defers_at_capacity_without_leaking_sessions() -> None:
    """A fire that hits the concurrency cap (429) must not leave an orphan chat."""
    release = asyncio.Event()
    block = _blocking_tool(release)
    store = ChatStore.in_memory()
    agent = _agent([call("block", {}, call_id="c1"), text("done")], tools=[block])
    app = create_app(agent, store=store, generate_titles=False, max_background_runs=1)
    deps = app.state.deps
    sched = Scheduler(deps)
    now = time.time()
    # 'a' sorts first (earlier next_fire) and grabs the only slot; 'b' hits the cap.
    await store.add_schedule(_row(id="a", next_fire=now - 2, now=now))
    await store.add_schedule(_row(id="b", input="second", next_fire=now - 1, now=now))

    try:
        await sched.run_due()  # a fires (blocks); b hits 429 and defers

        assert len(deps.supervisor._controllers) == 1  # only 'a' is live
        assert len(await store.list()) == 1  # 'b' left no orphan session row
        a = await store.get_schedule("a")
        b = await store.get_schedule("b")
        assert a is not None and a.next_fire > now  # advanced
        assert b is not None and b.next_fire == now - 1  # untouched → will retry
    finally:
        release.set()
        await _drain_runs(deps)


# --------------------------------------------------------------------------- #
# phase-3 API surface: edit, fetch, run-now
# --------------------------------------------------------------------------- #


def test_api_get_single_schedule() -> None:
    app = _app(_agent([text("hi")]), ChatStore.in_memory())
    c = TestClient(app)
    sid = c.post(
        "/api/schedules",
        json={"input": "ping", "trigger_kind": "every", "trigger_expr": "300"},
    ).json()["id"]
    assert c.get(f"/api/schedules/{sid}").json()["input"] == "ping"
    assert c.get("/api/schedules/nope").status_code == 404


def test_api_patch_edits_fields_and_recomputes_next_fire() -> None:
    app = _app(_agent([text("hi")]), ChatStore.in_memory())
    c = TestClient(app)
    created = c.post(
        "/api/schedules",
        json={
            "input": "ping",
            "session_id": "chat-1",
            "trigger_kind": "every",
            "trigger_expr": "300",
        },
    ).json()
    sid = created["id"]

    # Edit the prompt only: trigger untouched, next_fire unchanged.
    r = c.patch(f"/api/schedules/{sid}", json={"input": "pong"})
    assert r.status_code == 200, r.text
    assert r.json()["input"] == "pong"
    assert r.json()["next_fire"] == created["next_fire"]

    # Change the trigger: next_fire recomputed from now.
    r = c.patch(
        f"/api/schedules/{sid}",
        json={"trigger_kind": "every", "trigger_expr": "60"},
    )
    assert r.json()["trigger_expr"] == "60"
    assert 0 < r.json()["next_fire"] - time.time() <= 61

    # Explicit null detaches the session binding; omitting keeps it.
    r = c.patch(f"/api/schedules/{sid}", json={"session_id": None})
    assert r.json()["session_id"] is None
    r = c.patch(f"/api/schedules/{sid}", json={"input": "still pong"})
    assert r.json()["session_id"] is None

    # Bad edits are rejected without side effects.
    assert c.patch(f"/api/schedules/{sid}", json={"input": "  "}).status_code == 422
    assert (
        c.patch(
            f"/api/schedules/{sid}",
            json={"trigger_kind": "every", "trigger_expr": "0"},
        ).status_code
        == 422
    )
    assert c.patch(f"/api/schedules/{sid}", json={"agent": "ghost"}).status_code == 404
    assert c.get(f"/api/schedules/{sid}").json()["input"] == "still pong"


def test_api_stop_condition_fields_roundtrip() -> None:
    app = _app(_agent([text("hi")]), ChatStore.in_memory())
    c = TestClient(app)
    created = c.post(
        "/api/schedules",
        json={
            "input": "check log",
            "trigger_kind": "every",
            "trigger_expr": "60",
            "until": "it says ready",
            "max_fires": 10,
            "expires_at": 2_000_000_000,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    sid = body["id"]
    assert body["until"] == "it says ready"
    assert body["max_fires"] == 10
    assert body["expires_at"] == 2_000_000_000
    assert body["fire_count"] == 0 and body["finished_reason"] is None

    # Explicit null clears a field; the others are untouched.
    r = c.patch(f"/api/schedules/{sid}", json={"until": None})
    assert r.json()["until"] is None and r.json()["max_fires"] == 10

    # max_fires is validated (ge=1) on both create and patch.
    assert c.patch(f"/api/schedules/{sid}", json={"max_fires": 0}).status_code == 422
    assert (
        c.post(
            "/api/schedules",
            json={
                "input": "x",
                "trigger_kind": "every",
                "trigger_expr": "60",
                "max_fires": 0,
            },
        ).status_code
        == 422
    )


async def test_api_resume_clears_finished_reason() -> None:
    import httpx

    store = ChatStore.in_memory()
    app = _app(_agent([text("hi")]), store)
    now = time.time()
    await store.add_schedule(_row(id="s", next_fire=now + 60, now=now))
    await store.set_schedule_active("s", active=False, finished_reason="expired")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        row = (await ac.get("/api/schedules/s")).json()
        assert row["active"] is False and row["finished_reason"] == "expired"

        resumed = (await ac.patch("/api/schedules/s", json={"active": True})).json()
        assert resumed["active"] is True
        assert resumed["finished_reason"] is None  # live again, not "done"


@pytest.mark.asyncio
async def test_run_now_fires_and_advances_without_unpausing() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("done")]), store)
    deps = app.state.deps

    far = time.time() + 3600
    await store.add_schedule(_row(id="s1", next_fire=far, active=False))
    row = await store.get_schedule("s1")
    assert row is not None

    target = await Scheduler(deps).fire_now(row)
    assert target is not None
    # The fire ran headless into a fresh session…
    for _ in range(250):
        if deps.supervisor.get(target) is None:
            break
        await asyncio.sleep(0.02)
    assert (await store.session.load(target)) != []
    # …the cadence advanced, and the paused schedule stayed paused.
    after = await store.get_schedule("s1")
    assert after is not None
    assert after.active is False
    assert after.last_session_id == target


@pytest.mark.asyncio
async def test_run_now_endpoint_409s_while_previous_fire_is_live() -> None:
    import httpx

    release = asyncio.Event()

    @tool
    async def block() -> str:
        """Block until released."""
        await release.wait()
        return "ok"

    store = ChatStore.in_memory()
    app = _app(
        _agent([call("block", {}, call_id="c1"), text("done")], tools=[block]), store
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        sid = (
            await ac.post(
                "/api/schedules",
                json={"input": "go", "trigger_kind": "every", "trigger_expr": "3600"},
            )
        ).json()["id"]
        first = await ac.post(f"/api/schedules/{sid}/run")
        assert first.status_code == 200, first.text
        target = first.json()["session_id"]
        assert app.state.deps.supervisor.get(target) is not None
        # The live-runs surface ties the run back to its schedule (the UI's
        # "running" badge keys off this).
        live = next(
            r for r in (await ac.get("/api/runs")).json() if r["session_id"] == target
        )
        assert live["source"] == f"schedule:{sid}"

        # The previous fire is still running: run-now must refuse, not pile up.
        second = await ac.post(f"/api/schedules/{sid}/run")
        assert second.status_code == 409

        release.set()
        for _ in range(250):
            if app.state.deps.supervisor.get(target) is None:
                break
            await asyncio.sleep(0.02)
        assert (await ac.post(f"/api/schedules/{sid}/run")).status_code == 200
        # Let the second fire finish so shutdown doesn't cancel it mid-flight.
        for sid2, _ in list(app.state.deps.supervisor):
            for _ in range(250):
                if app.state.deps.supervisor.get(sid2) is None:
                    break
                await asyncio.sleep(0.02)


def test_cors_origins_enables_cross_origin_requests() -> None:
    app = create_app(
        _agent([text("hi")]),
        store=ChatStore.in_memory(),
        generate_titles=False,
        cors_origins=["http://localhost:5173"],
    )
    c = TestClient(app)
    r = c.get("/api/info", headers={"origin": "http://localhost:5173"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    # Origins not on the list get no CORS headers.
    r = c.get("/api/info", headers={"origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


# --------------------------------------------------------------------------- #
# run records (durable outcomes; schedule status + history derive from them)
# --------------------------------------------------------------------------- #


class _FailingProvider(ScriptedProvider):
    """Raise a non-retryable ProviderError on the first model call."""

    async def stream(self, entries, *, tools=None, response_format=None, settings=None):
        err = ProviderError("scripted terminal failure")
        err.retryable = False
        raise err
        yield  # pragma: no cover - makes this an async generator


async def _wait_outcome(store: ChatStore, source: str, timeout: float = 5.0):
    """The latest run record for ``source`` once it reaches a terminal status
    (the finalizing write lands in the run task's wind-down)."""
    deadline = time.time() + timeout
    while True:
        row = await store.latest_run_for(source)
        if row is not None and row.status != "running":
            return row
        if time.time() > deadline:  # pragma: no cover - failure path
            raise AssertionError("run outcome was never recorded")
        await asyncio.sleep(0.02)


async def test_fire_records_completed_run() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("done")]), store)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(_row(id="s", next_fire=now - 1, now=now))

    await Scheduler(deps).run_due()
    await _drain_runs(deps)

    rec = await _wait_outcome(store, "schedule:s")
    assert rec.status == "completed" and rec.error is None
    assert rec.agent == "bot"
    assert rec.finished_at is not None and rec.finished_at >= rec.started_at
    after = await store.get_schedule("s")
    assert after is not None and rec.session_id == after.last_session_id


async def test_fire_records_failed_run_with_message() -> None:
    store = ChatStore.in_memory()
    agent = Agent(name="bot", model=_FailingProvider([]))
    app = _app(agent, store)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(_row(id="s", next_fire=now - 1, now=now))

    await Scheduler(deps).run_due()
    await _drain_runs(deps)

    rec = await _wait_outcome(store, "schedule:s")
    assert rec.status == "failed"
    assert rec.error and "scripted terminal failure" in rec.error


async def test_fire_unavailable_agent_records_failed_record() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("hi")]), store)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(_row(id="s", agent="ghost", next_fire=now - 1, now=now))

    await Scheduler(deps).run_due()

    rec = await _wait_outcome(store, "schedule:s")
    assert rec.status == "failed"
    assert rec.error and "ghost" in rec.error
    assert rec.session_id is None  # the fire never reached a session
    after = await store.get_schedule("s")
    assert after is not None and after.next_fire > now  # advanced — no refire storm


async def test_fire_resolves_agent_self_name_to_registry_key() -> None:
    # The schedule_run tool stores agent.name; when the registry key differs
    # (create_app({"alias": agent})), the scheduler recovers by self-name.
    store = ChatStore.in_memory()
    agent = _agent([text("aliased ok")], name="inner-name")
    app = create_app({"alias": agent}, store=store, generate_titles=False)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(
        _row(id="s", agent="inner-name", next_fire=now - 1, now=now)
    )

    await Scheduler(deps).run_due()
    await _drain_runs(deps)

    rec = await _wait_outcome(store, "schedule:s")
    assert rec.status == "completed"
    # The run record and session metadata carry the registry key, not the
    # self-name.
    assert rec.agent == "alias"
    assert rec.session_id is not None
    meta = await store.get(rec.session_id)
    assert meta is not None and meta.agent == "alias"


def test_schedule_info_null_status_before_first_fire() -> None:
    store = ChatStore.in_memory()
    app = _app(_agent([text("hi")]), store)
    c = TestClient(app)
    created = c.post(
        "/api/schedules",
        json={"input": "do it", "trigger_kind": "every", "trigger_expr": "3600"},
    ).json()
    assert created["last_status"] is None and created["last_error"] is None


async def test_schedule_info_derives_status_and_history_from_records() -> None:
    import httpx

    store = ChatStore.in_memory()
    app = _app(_agent([text("one"), text("two")]), store)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(_row(id="s", next_fire=now - 1, now=now))

    sched = Scheduler(deps)
    await sched.run_due()
    await _drain_runs(deps)
    await _wait_outcome(store, "schedule:s")
    # Fire a second time so the history has an order to check.
    row = await store.get_schedule("s")
    assert row is not None
    await sched.fire_now(row)
    await _drain_runs(deps)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        info = next(
            s for s in (await ac.get("/api/schedules")).json() if s["id"] == "s"
        )
        assert info["last_status"] == "ok" and info["last_error"] is None

        history = (await ac.get("/api/schedules/s/runs")).json()
        assert len(history) == 2
        assert all(r["status"] == "completed" for r in history)
        assert all(r["source"] == "schedule:s" for r in history)
        # Newest first, each linked to the session it ran in.
        assert history[0]["started_at"] >= history[1]["started_at"]
        assert all(r["session_id"] for r in history)

        assert (await ac.get("/api/schedules/nope/runs")).status_code == 404

        # The general history endpoint filters by source too.
        by_source = (
            await ac.get("/api/runs/history", params={"source": "schedule:s"})
        ).json()
        assert [r["run_id"] for r in by_source] == [r["run_id"] for r in history]


async def test_pre_0_8_34_db_gains_stop_condition_columns(tmp_path) -> None:
    """A pre-0.8.34 DB whose ``chat_schedules`` lacks the stop-condition
    columns opens cleanly: the migration adds them and old rows read back with
    defaults. (The pre-0.8.27 ``schedules``-table fold was retired in 0.8.34 —
    such a legacy table is simply left alone.)"""
    import sqlite3
    from pathlib import Path

    path = Path(tmp_path) / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chat_schedules (
            id TEXT PRIMARY KEY,
            agent TEXT,
            input TEXT NOT NULL,
            session_id TEXT,
            trigger_kind TEXT NOT NULL,
            trigger_expr TEXT NOT NULL,
            next_fire REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            last_session_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO chat_schedules VALUES "
        "('s', 'bot', 'x', NULL, 'every', '3600', 1.0, 1, NULL, 1.0, 1.0)"
    )
    conn.commit()
    conn.close()

    store = ChatStore.sqlite(path)
    row = await store.get_schedule("s")
    assert row is not None and row.input == "x"
    assert row.until is None and row.max_fires is None and row.expires_at is None
    assert row.fire_count == 0 and row.finished_reason is None
    await store.add_schedule(
        _row(id="s2", now=2.0, next_fire=2.0, until="done", max_fires=1)
    )
    got = await store.get_schedule("s2")
    assert got is not None and got.until == "done" and got.max_fires == 1


async def test_cancelled_fire_records_cancelled_status() -> None:
    # A user stop surfaces internally as RunCancelled("user requested stop");
    # the record must read status "cancelled" with the stable "cancelled" text.
    release = asyncio.Event()
    store = ChatStore.in_memory()
    agent = _agent(
        [call("block", {}, call_id="c1"), text("done")],
        tools=[_blocking_tool(release)],
    )
    app = _app(agent, store)
    deps = app.state.deps
    now = time.time()
    await store.add_schedule(_row(id="s", next_fire=now - 1, now=now))

    await Scheduler(deps).run_due()
    after = await store.get_schedule("s")
    assert after is not None and after.last_session_id is not None
    sid = after.last_session_id
    await _wait_alive(deps, sid)
    deps.supervisor.cancel(sid)
    release.set()
    await _drain_runs(deps)

    rec = await _wait_outcome(store, "schedule:s")
    assert rec.status == "cancelled"
    assert rec.error == "cancelled"
