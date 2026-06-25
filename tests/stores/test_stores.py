from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from lovia.transcript import InputEntry, AssistantTextEntry
from lovia.stores import InMemorySession, SQLiteSession


async def test_in_memory_session() -> None:
    s = InMemorySession()
    await s.append("u1", [InputEntry(role="user", content="hi")])
    entries = await s.load("u1")
    assert len(entries) == 1
    assert isinstance(entries[0], InputEntry)
    assert entries[0].content == "hi"
    await s.clear("u1")
    assert await s.load("u1") == []


async def test_sqlite_session_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.db"
        s = SQLiteSession(path)
        await s.append(
            "u1",
            [
                InputEntry(role="user", content="hi"),
                AssistantTextEntry(content="hello"),
            ],
        )
        entries = await s.load("u1")
        assert [type(it).__name__ for it in entries] == [
            "InputEntry",
            "AssistantTextEntry",
        ]
        assert entries[0].content == "hi"  # type: ignore[union-attr]
        assert entries[1].content == "hello"  # type: ignore[union-attr]


# ------------------------------------------------------- InMemorySession ---


async def test_in_memory_append_accumulates_segments() -> None:
    s = InMemorySession()
    await s.append("u1", [InputEntry(role="user", content="one")])
    await s.append("u1", [AssistantTextEntry(content="two")])
    entries = await s.load("u1")
    # Append-only: each run is a segment; load concatenates them in order.
    assert [e.content for e in entries] == ["one", "two"]  # type: ignore[union-attr]


async def test_in_memory_sessions_are_isolated() -> None:
    s = InMemorySession()
    await s.append("a", [InputEntry(role="user", content="for-a")])
    await s.append("b", [InputEntry(role="user", content="for-b")])
    assert (await s.load("a"))[0].content == "for-a"  # type: ignore[union-attr]
    assert (await s.load("b"))[0].content == "for-b"  # type: ignore[union-attr]
    await s.clear("a")
    assert await s.load("a") == []
    assert len(await s.load("b")) == 1  # clearing one leaves the other


async def test_in_memory_load_returns_a_copy() -> None:
    s = InMemorySession()
    await s.append("u1", [InputEntry(role="user", content="hi")])
    got = await s.load("u1")
    got.append(InputEntry(role="user", content="injected"))
    # Mutating the returned list must not leak back into the store.
    assert len(await s.load("u1")) == 1


async def test_in_memory_append_copies_input_list() -> None:
    s = InMemorySession()
    src = [InputEntry(role="user", content="hi")]
    await s.append("u1", src)
    src.append(InputEntry(role="user", content="late"))
    # Mutating the caller's list after append must not affect the store.
    assert len(await s.load("u1")) == 1


# --------------------------------------------------------- SQLiteSession ---


async def test_sqlite_append_segments_and_clear() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = SQLiteSession(Path(tmp) / "s.db")
        await s.append("u1", [InputEntry(role="user", content="old")])
        await s.append("u1", [InputEntry(role="user", content="new")])
        entries = await s.load("u1")
        assert [e.content for e in entries] == ["old", "new"]  # type: ignore[union-attr]
        await s.clear("u1")
        assert await s.load("u1") == []


async def test_sqlite_append_persists_run_id_and_meta() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = SQLiteSession(Path(tmp) / "s.db")
        rid = await s.append(
            "u1", [InputEntry(role="user", content="x")], run_id="r1", meta={"k": "v"}
        )
        assert rid == "r1"
        # run_id is a first-class column; meta is opaque, persisted verbatim.
        conn = s._connect()
        try:
            row = conn.execute(
                "SELECT run_id, meta_json FROM session_runs WHERE session_id = ?",
                ("u1",),
            ).fetchone()
        finally:
            s._release(conn)
        assert row[0] == "r1"
        assert json.loads(row[1]) == {"k": "v"}


async def test_sqlite_append_is_idempotent_per_run_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = SQLiteSession(Path(tmp) / "s.db")
        await s.append("u1", [InputEntry(role="user", content="a")], run_id="r1")
        # Re-appending the same run_id is a no-op (first write wins).
        again = await s.append(
            "u1", [InputEntry(role="user", content="DUP")], run_id="r1"
        )
        assert again == "r1"
        assert [e.content for e in await s.load("u1")] == ["a"]  # type: ignore[union-attr]


async def test_append_generates_run_id_when_absent() -> None:
    s = InMemorySession()
    r1 = await s.append("u1", [InputEntry(role="user", content="one")])
    r2 = await s.append("u1", [InputEntry(role="user", content="two")])
    # Each omitted run_id gets a distinct generated id; both segments persist.
    assert r1 and r2 and r1 != r2
    assert [e.content for e in await s.load("u1")] == ["one", "two"]  # type: ignore[union-attr]


async def test_sqlite_sessions_are_isolated_and_ordered() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = SQLiteSession(Path(tmp) / "s.db")
        await s.append("a", [InputEntry(role="user", content="a1")])
        await s.append("b", [InputEntry(role="user", content="b1")])
        await s.append("a", [AssistantTextEntry(content="a2")])
        # Order preserved across separate appends; sessions don't bleed.
        assert [e.content for e in await s.load("a")] == ["a1", "a2"]  # type: ignore[union-attr]
        assert [e.content for e in await s.load("b")] == ["b1"]  # type: ignore[union-attr]


async def test_sqlite_memory_path_shares_one_connection() -> None:
    # ":memory:" must hold a single connection open, otherwise each call
    # would see a brand-new empty database.
    s = SQLiteSession(":memory:")
    await s.append("u1", [InputEntry(role="user", content="persisted")])
    entries = await s.load("u1")
    assert [e.content for e in entries] == ["persisted"]  # type: ignore[union-attr]


# ----------------------------------------------------- segments() primitive ---


@pytest.mark.parametrize("make", [lambda: InMemorySession(), lambda: SQLiteSession(":memory:")])
async def test_segments_round_trip_run_id_and_meta(make) -> None:
    s = make()
    await s.append(
        "u1", [InputEntry(role="user", content="one")], run_id="r1", meta={"m": 1}
    )
    await s.append("u1", [InputEntry(role="user", content="two")], run_id="r2")

    segs = await s.segments("u1")
    assert [seg.run_id for seg in segs] == ["r1", "r2"]
    assert [seg.meta for seg in segs] == [{"m": 1}, None]
    assert [e.content for seg in segs for e in seg.entries] == ["one", "two"]  # type: ignore[union-attr]

    # load() (inherited default) flattens segments identically.
    assert [e.content for e in await s.load("u1")] == ["one", "two"]  # type: ignore[union-attr]


async def test_in_memory_segments_returns_a_copy() -> None:
    s = InMemorySession()
    await s.append("u1", [InputEntry(role="user", content="x")])
    segs = await s.segments("u1")
    segs[0].entries.append(InputEntry(role="user", content="leak"))
    # Mutating the returned segment must not corrupt stored state.
    assert len(await s.load("u1")) == 1
