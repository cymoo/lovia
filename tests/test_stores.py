from __future__ import annotations

import tempfile
from pathlib import Path

from lovia import ChatMessage
from lovia.stores import InMemorySession, SQLiteSession


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
