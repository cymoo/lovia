"""Tests for the model-driven ``schedule_run`` tool (``lovia.web.scheduling``).

Two layers: unit tests call the tool directly with a hand-built ``RunContext``
(fast, no runner), and integration tests drive ``/api/chat/stream`` so the
``needs_approval`` gate and the ``GET /api/schedules`` contract are exercised
end to end. A final test feeds a tool-created row through the real ``Scheduler``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from lovia import Agent  # noqa: E402
from lovia.run_context import RunContext  # noqa: E402
from lovia.web import ChatStore, create_app  # noqa: E402
from lovia.web.scheduler import Scheduler  # noqa: E402
from lovia.web.scheduling import Scheduling, _make_tool, _to_epoch  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_scheduler import _drain_runs  # noqa: E402
from .test_supervisor import _client, _spawn, _wait_run  # noqa: E402


def _ctx(
    *, session_id: str | None = "s1", agent_name: str | None = "bot"
) -> RunContext:
    """A minimal RunContext exposing only what the tool reads (agent, session)."""
    agent = SimpleNamespace(name=agent_name) if agent_name is not None else None
    return RunContext(context=None, entries=[], agent=agent, session_id=session_id)


async def _invoke(
    store: ChatStore, args: dict, *, ctx: RunContext | None = None
) -> str:
    return await _make_tool(store).invoke(args, ctx or _ctx())


# --------------------------------------------------------------------------- #
# unit: trigger normalization
# --------------------------------------------------------------------------- #


def test_to_epoch_passes_through_epoch() -> None:
    assert _to_epoch("1700000000") == "1700000000"
    assert _to_epoch("1700000000.5") == "1700000000.5"


def test_to_epoch_converts_iso() -> None:
    # A naive ISO datetime is interpreted in local time → a positive epoch.
    assert float(_to_epoch("2033-05-18T03:33")) > 1_900_000_000


def test_to_epoch_handles_z_suffix() -> None:
    # Models often emit `Z` for UTC; datetime.fromisoformat rejects it on <3.11.
    expected = datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc).timestamp()
    assert float(_to_epoch("2026-06-29T09:00Z")) == expected


def test_to_epoch_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        _to_epoch("not-a-time")


# --------------------------------------------------------------------------- #
# unit: the tool
# --------------------------------------------------------------------------- #


def test_tool_requires_approval() -> None:
    assert _make_tool(ChatStore.in_memory()).needs_approval is True


@pytest.mark.asyncio
async def test_creates_one_schedule_row() -> None:
    store = ChatStore.in_memory()
    out = await _invoke(
        store,
        {
            "instruction": "water the plants",
            "trigger_kind": "every",
            "trigger_expr": "300",
        },
    )
    rows = await store.list_schedules()
    assert len(rows) == 1
    (row,) = rows
    assert row.agent == "bot"  # pinned to the active agent
    assert row.input == "water the plants"
    assert row.trigger_kind == "every"
    assert row.trigger_expr == "300"
    assert row.active is True
    assert row.session_id == "s1"  # continues THIS conversation by default
    assert row.last_session_id is None
    assert row.next_fire == pytest.approx(row.created_at + 300, abs=2.0)
    assert "Scheduled" in out


@pytest.mark.asyncio
async def test_continue_session_pins_current_session() -> None:
    store = ChatStore.in_memory()
    await _invoke(
        store,
        {
            "instruction": "follow up",
            "trigger_kind": "every",
            "trigger_expr": "300",
            "continue_session": True,
        },
        ctx=_ctx(session_id="sess-42"),
    )
    (row,) = await store.list_schedules()
    assert row.session_id == "sess-42"


@pytest.mark.asyncio
async def test_continue_session_false_starts_fresh() -> None:
    # The opt-out: continue_session=False forces a fresh session per fire even
    # when this run has a session.
    store = ChatStore.in_memory()
    await _invoke(
        store,
        {
            "instruction": "digest",
            "trigger_kind": "every",
            "trigger_expr": "300",
            "continue_session": False,
        },
        ctx=_ctx(session_id="sess-42"),
    )
    (row,) = await store.list_schedules()
    assert row.session_id is None


@pytest.mark.asyncio
async def test_continue_session_without_a_session_stays_fresh() -> None:
    # continue_session=True but the run has no session → fall back to fresh.
    store = ChatStore.in_memory()
    await _invoke(
        store,
        {
            "instruction": "x",
            "trigger_kind": "every",
            "trigger_expr": "300",
            "continue_session": True,
        },
        ctx=_ctx(session_id=None),
    )
    (row,) = await store.list_schedules()
    assert row.session_id is None


@pytest.mark.asyncio
async def test_at_accepts_both_iso_and_epoch() -> None:
    store = ChatStore.in_memory()
    await _invoke(
        store,
        {"instruction": "epoch", "trigger_kind": "at", "trigger_expr": "2000000000"},
    )
    await _invoke(
        store,
        {
            "instruction": "iso",
            "trigger_kind": "at",
            "trigger_expr": "2033-05-18T03:33",
        },
    )
    by_input = {r.input: r for r in await store.list_schedules()}
    assert float(by_input["epoch"].trigger_expr) == 2_000_000_000.0
    assert float(by_input["iso"].trigger_expr) > 1_900_000_000
    assert by_input["iso"].trigger_kind == "at"


@pytest.mark.asyncio
async def test_pins_agent_from_context() -> None:
    store = ChatStore.in_memory()
    await _invoke(
        store,
        {"instruction": "x", "trigger_kind": "every", "trigger_expr": "300"},
        ctx=_ctx(agent_name="researcher"),
    )
    (row,) = await store.list_schedules()
    assert row.agent == "researcher"


@pytest.mark.asyncio
async def test_invalid_interval_creates_no_row() -> None:
    store = ChatStore.in_memory()
    out = await _invoke(
        store, {"instruction": "x", "trigger_kind": "every", "trigger_expr": "0"}
    )
    assert "couldn't schedule" in out.lower()
    assert len(await store.list_schedules()) == 0


@pytest.mark.asyncio
async def test_unparseable_at_time_creates_no_row() -> None:
    store = ChatStore.in_memory()
    out = await _invoke(
        store, {"instruction": "x", "trigger_kind": "at", "trigger_expr": "whenever"}
    )
    assert "at" in out.lower()  # the friendly "couldn't parse the 'at' time" hint
    assert len(await store.list_schedules()) == 0


@pytest.mark.asyncio
async def test_empty_instruction_is_refused() -> None:
    store = ChatStore.in_memory()
    out = await _invoke(
        store, {"instruction": "   ", "trigger_kind": "every", "trigger_expr": "300"}
    )
    assert "empty" in out.lower()
    assert len(await store.list_schedules()) == 0


@pytest.mark.asyncio
async def test_plugin_contributes_tool_and_instructions() -> None:
    inst = await Scheduling(ChatStore.in_memory()).setup()
    assert {t.name for t in inst.tools} == {"schedule_run"}
    assert inst.instructions  # non-empty guidance steers the model


# --------------------------------------------------------------------------- #
# integration: the approval gate + the GET /api/schedules contract
# --------------------------------------------------------------------------- #


def _scheduling_app(store: ChatStore):
    provider = ScriptedProvider(
        [
            call(
                "schedule_run",
                {
                    "instruction": "water the plants",
                    "trigger_kind": "every",
                    "trigger_expr": "3600",
                },
                call_id="c1",
            ),
            text("done"),
        ]
    )
    agent = Agent(name="bot", model=provider, plugins=[Scheduling(store)])
    return create_app(agent, store=store, generate_titles=False)


@pytest.mark.asyncio
async def test_stream_approve_creates_schedule() -> None:
    store = ChatStore.in_memory()
    app = _scheduling_app(store)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "please", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", status="blocked_on_approval")  # gated, not auto-run
        await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "approve"},
        )
        await asyncio.wait_for(task, timeout=5)
        rows = (await ac.get("/api/schedules")).json()
    assert len(rows) == 1
    assert rows[0]["input"] == "water the plants"
    assert rows[0]["agent"] == "bot"
    assert rows[0]["trigger_kind"] == "every"


@pytest.mark.asyncio
async def test_stream_deny_creates_nothing() -> None:
    store = ChatStore.in_memory()
    app = _scheduling_app(store)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "please", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", status="blocked_on_approval")
        await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "deny"},
        )
        await asyncio.wait_for(task, timeout=5)
        rows = (await ac.get("/api/schedules")).json()
    assert rows == []


# --------------------------------------------------------------------------- #
# integration: a tool-created row actually fires through the Scheduler
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tool_created_row_fires_via_scheduler() -> None:
    store = ChatStore.in_memory()
    provider = ScriptedProvider([text("fired output")])
    agent = Agent(name="bot", model=provider, plugins=[Scheduling(store)])
    app = create_app(agent, store=store, generate_titles=False)
    deps = app.state.deps

    # A one-shot 'at' a second in the past → immediately due.
    await _invoke(
        store,
        {
            "instruction": "do the thing",
            "trigger_kind": "at",
            "trigger_expr": str(time.time() - 1),
        },
    )
    await Scheduler(deps).run_due()
    await _drain_runs(deps)

    (row,) = await store.list_schedules()
    assert row.active is False  # one-shot deactivated after firing
    assert row.last_session_id is not None  # it fired into a fresh session
    assert provider.calls  # the scheduled agent actually ran
