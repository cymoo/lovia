"""SQLite-backed :class:`Session`.

Uses :mod:`sqlite3` from the stdlib via :func:`asyncio.to_thread` so we don't
add ``aiosqlite`` as a dependency. Concurrency is serialized through a single
async lock; that's plenty for the kind of workloads agent frameworks see.

The schema is intentionally trivial: messages are stored as JSON blobs in
insertion order. Loading deserializes them back into :class:`ChatMessage`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from ..messages import ChatMessage, ToolCall


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT (julianday('now'))
);
CREATE INDEX IF NOT EXISTS idx_session_messages_sid
    ON session_messages(session_id, id);
"""


class _SQLiteBase:
    """Shared connection/lock plumbing."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = asyncio.Lock()
        self._initialized = False

    async def _connect(self) -> sqlite3.Connection:
        # ``check_same_thread=False`` is safe here because the lock serializes
        # all access from a single asyncio loop.
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not self._initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            self._initialized = True
        return conn

    async def _run(self, fn):  # type: ignore[no-untyped-def]
        async with self._lock:
            return await asyncio.to_thread(fn)


class SQLiteSession(_SQLiteBase):
    """A :class:`Session` persisted to a SQLite file."""

    async def load(self, session_id: str) -> list[ChatMessage]:
        def _impl() -> list[ChatMessage]:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            try:
                conn.executescript(_SCHEMA)
                rows = conn.execute(
                    "SELECT payload FROM session_messages WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
                return [_deserialize(json.loads(r[0])) for r in rows]
            finally:
                conn.close()

        return await self._run(_impl)

    async def append(self, session_id: str, messages: list[ChatMessage]) -> None:
        def _impl() -> None:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            try:
                conn.executescript(_SCHEMA)
                conn.executemany(
                    "INSERT INTO session_messages (session_id, payload) VALUES (?, ?)",
                    [(session_id, json.dumps(_serialize(m))) for m in messages],
                )
                conn.commit()
            finally:
                conn.close()

        await self._run(_impl)

    async def clear(self, session_id: str) -> None:
        def _impl() -> None:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            try:
                conn.executescript(_SCHEMA)
                conn.execute(
                    "DELETE FROM session_messages WHERE session_id = ?", (session_id,)
                )
                conn.commit()
            finally:
                conn.close()

        await self._run(_impl)


def _serialize(msg: ChatMessage) -> dict:
    return {
        "role": msg.role,
        "content": msg.content,
        "tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in msg.tool_calls
        ],
        "tool_call_id": msg.tool_call_id,
        "name": msg.name,
        "reasoning_content": msg.reasoning_content,
    }


def _deserialize(d: dict) -> ChatMessage:
    return ChatMessage(
        role=d["role"],
        content=d.get("content"),
        tool_calls=[
            ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
            for tc in d.get("tool_calls") or []
        ],
        tool_call_id=d.get("tool_call_id"),
        name=d.get("name"),
        reasoning_content=d.get("reasoning_content"),
    )
