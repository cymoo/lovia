from __future__ import annotations

import tempfile
from pathlib import Path

from lovia import InputMessageItem, MessageOutputItem
from lovia.stores import InMemorySession, SQLiteSession


async def test_in_memory_session() -> None:
    s = InMemorySession()
    await s.append("u1", [InputMessageItem(role="user", content="hi")])
    items = await s.load("u1")
    assert len(items) == 1
    assert isinstance(items[0], InputMessageItem)
    assert items[0].content == "hi"
    await s.clear("u1")
    assert await s.load("u1") == []


async def test_sqlite_session_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.db"
        s = SQLiteSession(path)
        await s.append(
            "u1",
            [
                InputMessageItem(role="user", content="hi"),
                MessageOutputItem(content="hello"),
            ],
        )
        items = await s.load("u1")
        assert [type(it).__name__ for it in items] == [
            "InputMessageItem",
            "MessageOutputItem",
        ]
        assert items[0].content == "hi"  # type: ignore[union-attr]
        assert items[1].content == "hello"  # type: ignore[union-attr]
