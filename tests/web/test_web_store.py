"""Tests for ChatStore: metadata + title management."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from lovia.transcript import TranscriptEntry, AssistantTextEntry
from lovia.web import ChatStore
from lovia.web.store import RunRow, ScheduleRow


async def test_in_memory_roundtrip() -> None:
    store = ChatStore.in_memory()
    assert await store.list() == []
    await store.upsert("s1", agent="bot")
    metas = await store.list()
    assert len(metas) == 1
    assert metas[0].id == "s1"
    assert metas[0].agent == "bot"
    assert metas[0].title is None


async def test_set_title_and_truncate() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1")
    await store.set_title("s1", "  Hello World  " + "x" * 200)
    meta = await store.get("s1")
    assert meta is not None
    assert meta.title is not None
    assert meta.title.startswith("Hello World")
    assert len(meta.title) <= 120


async def test_set_title_if_unchanged_applies_when_provisional_intact() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1", title="Provisional")
    await store.set_title_if_unchanged("s1", "Generated Title", expected="Provisional")
    meta = await store.get("s1")
    assert meta is not None
    assert meta.title == "Generated Title"


async def test_set_title_if_unchanged_skips_after_manual_rename() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1", title="Provisional")
    # User renames before the background-generated title lands.
    await store.set_title("s1", "My Name")
    await store.set_title_if_unchanged("s1", "Generated Title", expected="Provisional")
    meta = await store.get("s1")
    assert meta is not None
    assert meta.title == "My Name"  # not clobbered


async def test_upsert_bumps_updated_at() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1")
    first = await store.get("s1")
    assert first is not None
    await store.upsert("s1")
    second = await store.get("s1")
    assert second is not None
    assert second.updated_at >= first.updated_at
    assert second.created_at == first.created_at


async def test_list_orders_by_updated_at_desc() -> None:
    store = ChatStore.in_memory()
    await store.upsert("old")
    await store.upsert("new")
    await store.upsert("old")  # bump old's updated_at
    ids = [m.id for m in await store.list()]
    assert ids == ["old", "new"]


async def test_list_and_search_paginate_with_offset() -> None:
    store = ChatStore.in_memory()
    for i in range(5):
        await store.upsert(f"s{i}", title=f"Chat {i}")

    # Most recent first: s4 … s0; offset walks down that order.
    assert [m.id for m in await store.list(limit=2)] == ["s4", "s3"]
    assert [m.id for m in await store.list(limit=2, offset=2)] == ["s2", "s1"]
    assert [m.id for m in await store.list(limit=2, offset=4)] == ["s0"]
    assert await store.list(limit=2, offset=5) == []

    # Search pages the same way.
    hits = [m.id for m in await store.search("Chat", limit=2, offset=2)]
    assert hits == ["s2", "s1"]


async def test_delete_removes_transcript_and_meta(tmp_path: Path) -> None:
    store = ChatStore.sqlite(tmp_path / "x.db")
    await store.upsert("s1")
    msg: TranscriptEntry = AssistantTextEntry(content="hi")
    await store.session.append("s1", [msg])
    assert (await store.session.load("s1")) != []
    assert (await store.get("s1")) is not None

    await store.delete("s1")
    assert (await store.session.load("s1")) == []
    assert (await store.get("s1")) is None


async def test_delete_drops_the_sessions_checkpoint(tmp_path: Path) -> None:
    """Deleting a chat must not strand its interrupted run's snapshot: once the
    metadata row is gone, ``active_run_id`` is unreadable and the checkpoint would
    leak forever (unreachable, never resumable, never cleaned up)."""
    from lovia.checkpointer import RunHead
    from lovia.messages import Usage

    store = ChatStore.sqlite(tmp_path / "x.db")
    assert store.checkpointer is not None
    await store.upsert("s1")
    await store.checkpointer.append(
        "run-1",
        [AssistantTextEntry(content="partial")],
        RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
    )
    await store.set_active_run_id("s1", "run-1")
    assert (await store.checkpointer.load("run-1")) is not None

    await store.delete("s1")
    assert (await store.checkpointer.load("run-1")) is None  # no orphan left behind


async def test_delete_all_drops_checkpoints(tmp_path: Path) -> None:
    from lovia.checkpointer import RunHead
    from lovia.messages import Usage

    store = ChatStore.sqlite(tmp_path / "x.db")
    assert store.checkpointer is not None
    for sid, rid in (("s1", "run-1"), ("s2", "run-2")):
        await store.upsert(sid)
        await store.checkpointer.append(
            rid,
            [AssistantTextEntry(content="partial")],
            RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
        )
        await store.set_active_run_id(sid, rid)

    await store.delete_all()
    assert (await store.checkpointer.load("run-1")) is None
    assert (await store.checkpointer.load("run-2")) is None
    assert (await store.list()) == []


async def test_sqlite_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    s1 = ChatStore.sqlite(path)
    await s1.upsert("s1", agent="bot")
    await s1.set_title("s1", "First chat")

    s2 = ChatStore.sqlite(path)
    meta = await s2.get("s1")
    assert meta is not None
    assert meta.title == "First chat"
    assert meta.agent == "bot"


async def test_get_missing_returns_none() -> None:
    store = ChatStore.in_memory()
    assert (await store.get("nope")) is None


@pytest.mark.parametrize("agent", [None, "alpha"])
async def test_upsert_with_optional_agent(agent: str | None) -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1", agent=agent)
    meta = await store.get("s1")
    assert meta is not None
    assert meta.agent == agent


# ---- pinning -------------------------------------------------------------


async def test_pinned_defaults_to_false() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1")
    meta = await store.get("s1")
    assert meta is not None
    assert meta.pinned is False


async def test_set_pinned_roundtrip() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1")
    await store.set_pinned("s1", True)
    meta = await store.get("s1")
    assert meta is not None and meta.pinned is True
    await store.set_pinned("s1", False)
    meta = await store.get("s1")
    assert meta is not None and meta.pinned is False


async def test_list_orders_pinned_first() -> None:
    store = ChatStore.in_memory()
    await store.upsert("old")
    await store.upsert("mid")
    await store.upsert("new")  # newest by updated_at
    # Pin the oldest — it must jump to the top despite being least recent.
    await store.set_pinned("old", True)
    ids = [m.id for m in await store.list()]
    assert ids == ["old", "new", "mid"]


async def test_search_orders_pinned_first() -> None:
    store = ChatStore.in_memory()
    await store.upsert("a", title="alpha one")
    await store.upsert("b", title="alpha two")  # more recent
    await store.set_pinned("a", True)
    ids = [m.id for m in await store.search("alpha")]
    assert ids == ["a", "b"]


async def test_pinned_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "pin.db"
    s1 = ChatStore.sqlite(path)
    await s1.upsert("s1")
    await s1.set_pinned("s1", True)

    s2 = ChatStore.sqlite(path)
    meta = await s2.get("s1")
    assert meta is not None and meta.pinned is True


async def test_migration_backfills_pinned_on_legacy_db(tmp_path: Path) -> None:
    """A DB created before ``pinned`` existed must gain the column on open."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE chat_sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            agent TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            active_run_id TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO chat_sessions (id, title, agent, created_at, updated_at) "
        "VALUES ('legacy', 'Old chat', 'bot', 1.0, 2.0)"
    )
    conn.commit()
    conn.close()

    # Opening the store runs the idempotent migration (ALTER + index).
    store = ChatStore.sqlite(path)
    meta = await store.get("legacy")
    assert meta is not None
    assert meta.pinned is False  # back-filled default
    assert meta.title == "Old chat"  # existing data intact

    # The new column is writable, and a second open is a no-op (no error).
    await store.set_pinned("legacy", True)
    again = ChatStore.sqlite(path)
    meta = await again.get("legacy")
    assert meta is not None and meta.pinned is True


async def test_chat_store_wal_covers_all_three_stores(tmp_path: Path) -> None:
    # ChatStore.sqlite(wal=True) points session, checkpointer, and metadata at
    # one WAL-mode file; everything still round-trips.
    from lovia.checkpointer import RunHead
    from lovia.messages import Usage

    store = ChatStore.sqlite(tmp_path / "x.db", wal=True)
    await store.upsert("s1", agent="bot")
    await store.session.append("s1", [AssistantTextEntry(content="hi")])
    assert store.checkpointer is not None
    await store.checkpointer.append(
        "run-1", [], RunHead(agent_name="bot", usage=Usage(), turns=1)
    )

    assert (await store.get("s1")) is not None
    assert len(await store.session.load("s1")) == 1
    assert (await store.checkpointer.load("run-1")) is not None
    with store._meta._conn() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


async def test_delete_all_clears_beyond_one_list_page() -> None:
    # delete_all must sweep EVERY session — not just the first list() page —
    # or transcripts/checkpoints past the page limit are orphaned while their
    # metadata rows vanish.
    store = ChatStore.in_memory()
    n = 230  # > the 200-row default list page
    for i in range(n):
        sid = f"s{i:03d}"
        await store.upsert(sid)
        await store.session.append(sid, [AssistantTextEntry(content=f"m{i}")])
    await store.delete_all()
    assert await store.list(limit=1000) == []
    for sid in ("s000", "s150", f"s{n - 1:03d}"):
        assert await store.session.load(sid) == []


async def test_search_treats_like_wildcards_literally() -> None:
    store = ChatStore.in_memory()
    await store.upsert("a", title="Progress: 100% done")
    await store.upsert("b", title="under_score name")
    await store.upsert("c", title="plain title")

    assert [m.id for m in await store.search("100%")] == ["a"]
    assert [m.id for m in await store.search("under_score")] == ["b"]
    assert await store.search("100_") == []  # _ is literal, not any-char
    # A lone backslash in the query must not break the ESCAPE clause.
    assert await store.search("\\") == []


# --------------------------------------------------------------------------- #
# run records
# --------------------------------------------------------------------------- #


def _run_row(**kw) -> RunRow:
    fields: dict = {
        "id": "r1",
        "session_id": "s1",
        "agent": "bot",
        "source": "user",
        "status": "running",
        "error": None,
        "started_at": 100.0,
        "finished_at": None,
        "usage": None,
    }
    fields.update(kw)
    return RunRow(**fields)


async def test_run_record_start_finish_roundtrip() -> None:
    store = ChatStore.in_memory()
    await store.start_run(_run_row())
    await store.finish_run(
        "r1", status="completed", usage={"input_tokens": 3, "output_tokens": 5}
    )

    rec = await store.latest_run_for("user")
    assert rec is not None
    assert rec.status == "completed" and rec.error is None
    assert rec.finished_at is not None
    assert rec.usage == {"input_tokens": 3, "output_tokens": 5}


async def test_run_record_resume_keeps_identity() -> None:
    # Resuming an interrupted run re-inserts the same id: the row flips back to
    # "running" but keeps its original started_at and source.
    store = ChatStore.in_memory()
    await store.start_run(_run_row(source="schedule:s", started_at=100.0))
    await store.finish_run("r1", status="interrupted", error="cancelled")
    await store.start_run(_run_row(source="user", started_at=200.0))

    rec = await store.latest_run_for("schedule:s")
    assert rec is not None
    assert rec.status == "running" and rec.error is None and rec.finished_at is None
    assert rec.started_at == 100.0 and rec.source == "schedule:s"


async def test_list_runs_filters_and_orders() -> None:
    store = ChatStore.in_memory()
    await store.start_run(_run_row(id="a", session_id="s1", started_at=10.0))
    await store.start_run(
        _run_row(id="b", session_id="s2", source="schedule:x", started_at=20.0)
    )
    await store.start_run(_run_row(id="c", session_id="s1", started_at=30.0))
    await store.finish_run("a", status="completed")

    assert [r.id for r in await store.list_runs()] == ["c", "b", "a"]
    assert [r.id for r in await store.list_runs(session_id="s1")] == ["c", "a"]
    assert [r.id for r in await store.list_runs(source="schedule:x")] == ["b"]
    # ``since`` keeps only runs finished after the cutoff (unfinished excluded).
    assert [r.id for r in await store.list_runs(since=0.0)] == ["a"]
    assert await store.list_runs(since=time.time() + 60) == []
    assert [r.id for r in await store.list_runs(limit=1, offset=1)] == ["b"]


async def test_sweep_stale_runs_marks_running_interrupted() -> None:
    store = ChatStore.in_memory()
    await store.start_run(_run_row(id="a"))
    await store.start_run(_run_row(id="b", started_at=50.0))
    await store.finish_run("b", status="completed")

    await store.sweep_stale_runs()

    a = await store.latest_run_for("user")  # newest first → "a" (started later)
    assert a is not None and a.id == "a"
    assert a.status == "interrupted" and a.finished_at is not None
    runs = {r.id: r.status for r in await store.list_runs()}
    assert runs == {"a": "interrupted", "b": "completed"}  # terminal untouched


async def test_delete_session_drops_its_run_records() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1")
    await store.start_run(_run_row(id="a", session_id="s1"))
    await store.start_run(_run_row(id="b", session_id="s2"))

    await store.delete("s1")
    assert [r.id for r in await store.list_runs()] == ["b"]

    await store.delete_all()
    assert await store.list_runs() == []


async def test_delete_schedule_drops_its_run_records() -> None:
    store = ChatStore.in_memory()
    now = time.time()
    await store.add_schedule(
        ScheduleRow(
            id="x",
            agent=None,
            input="go",
            session_id=None,
            trigger_kind="every",
            trigger_expr="60",
            next_fire=now,
            active=True,
            last_session_id=None,
            created_at=now,
            updated_at=now,
        )
    )
    await store.start_run(_run_row(id="a", source="schedule:x"))
    await store.start_run(_run_row(id="b", source="user"))

    await store.delete_schedule("x")
    assert [r.id for r in await store.list_runs()] == ["b"]


async def test_migration_folds_legacy_schedules_into_chat_schedules(
    tmp_path: Path,
) -> None:
    # Pre-0.8.27 DBs name the table ``schedules`` (some with the retired
    # last_status/last_error columns). Opening one moves the rows to
    # ``chat_schedules`` and drops the old table — dead columns and all.
    # Idempotent: reopening must not duplicate or fail.
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schedules (id TEXT PRIMARY KEY, agent TEXT, "
        "input TEXT NOT NULL, session_id TEXT, trigger_kind TEXT NOT NULL, "
        "trigger_expr TEXT NOT NULL, next_fire REAL NOT NULL, "
        "active INTEGER NOT NULL DEFAULT 1, last_session_id TEXT, "
        "last_status TEXT, last_error TEXT, "
        "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schedules VALUES ('x', 'bot', 'go', NULL, 'every', "
        "'3600', 1.0, 1, NULL, 'ok', NULL, 1.0, 1.0)"
    )
    conn.commit()
    conn.close()

    for _ in range(2):
        store = ChatStore.sqlite(path)
        row = await store.get_schedule("x")
        assert row is not None and row.input == "go" and row.active
        assert len(await store.list_schedules()) == 1

    conn = sqlite3.connect(path)
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert "chat_schedules" in names and "schedules" not in names
