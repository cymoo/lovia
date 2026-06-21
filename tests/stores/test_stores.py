from __future__ import annotations

import tempfile
from pathlib import Path

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


async def test_in_memory_replace_overwrites() -> None:
    s = InMemorySession()
    await s.append("u1", [InputEntry(role="user", content="old")])
    await s.replace("u1", [InputEntry(role="user", content="new")])
    entries = await s.load("u1")
    assert len(entries) == 1
    assert entries[0].content == "new"  # type: ignore[union-attr]


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


async def test_in_memory_replace_copies_input_list() -> None:
    s = InMemorySession()
    src = [InputEntry(role="user", content="hi")]
    await s.replace("u1", src)
    src.append(InputEntry(role="user", content="late"))
    # Mutating the caller's list after replace must not affect the store.
    assert len(await s.load("u1")) == 1


# --------------------------------------------------------- SQLiteSession ---


async def test_sqlite_replace_and_clear() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = SQLiteSession(Path(tmp) / "s.db")
        await s.append("u1", [InputEntry(role="user", content="old")])
        await s.replace("u1", [InputEntry(role="user", content="new")])
        entries = await s.load("u1")
        assert [e.content for e in entries] == ["new"]  # type: ignore[union-attr]
        await s.clear("u1")
        assert await s.load("u1") == []


async def test_sqlite_replace_to_empty_clears() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = SQLiteSession(Path(tmp) / "s.db")
        await s.append("u1", [InputEntry(role="user", content="x")])
        await s.replace("u1", [])
        assert await s.load("u1") == []


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
