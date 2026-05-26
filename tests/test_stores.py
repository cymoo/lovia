from __future__ import annotations

import tempfile
from pathlib import Path

from lovia import ChatMessage
from lovia.stores import (
    InMemoryMemoryStore,
    InMemorySession,
    SQLiteMemoryStore,
    SQLiteSession,
)


async def test_in_memory_session() -> None:
    s = InMemorySession()
    await s.append("u1", [ChatMessage(role="user", content="hi")])
    msgs = await s.load("u1")
    assert len(msgs) == 1 and msgs[0].content == "hi"
    await s.clear("u1")
    assert await s.load("u1") == []


async def test_sqlite_session_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.db"
        s = SQLiteSession(path)
        await s.append(
            "u1",
            [
                ChatMessage(role="user", content="hi"),
                ChatMessage(role="assistant", content="hello"),
            ],
        )
        msgs = await s.load("u1")
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[0].content == "hi"


async def test_in_memory_memory_store() -> None:
    m = InMemoryMemoryStore()
    await m.set("user:alice:nickname", "Ally")
    assert await m.get("user:alice:nickname") == "Ally"
    listing = await m.list("user:alice")
    assert listing == [("user:alice:nickname", "Ally")]
    await m.delete("user:alice:nickname")
    assert await m.get("user:alice:nickname") is None


async def test_sqlite_memory_store() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.db"
        m = SQLiteMemoryStore(path)
        await m.set("k", "v")
        await m.set("k", "v2")  # upsert
        assert await m.get("k") == "v2"
        assert await m.list() == [("k", "v2")]
