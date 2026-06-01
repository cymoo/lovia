from __future__ import annotations

import tempfile
from pathlib import Path

from lovia import InputEntry, AssistantTextEntry
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
