"""Session implementations: in-memory and SQLite-backed.

:class:`InMemorySession` keeps transcripts in process — suitable for tests,
CLIs, or single-process applications. :class:`SQLiteSession` persists entries
to a SQLite file via the stdlib :mod:`sqlite3` driver and
:func:`asyncio.to_thread`; no extra dependencies required.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path

from ..transcript import TranscriptEntry, entry_from_dict, entry_to_dict
from ._sqlite import SQLiteStore


class InMemorySession:
    """A :class:`~lovia.session.Session` that keeps transcripts in a dict."""

    def __init__(self) -> None:
        self._data: dict[str, list[TranscriptEntry]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def load(self, session_id: str) -> list[TranscriptEntry]:
        async with self._lock:
            return list(self._data.get(session_id, []))

    async def append(self, session_id: str, entries: list[TranscriptEntry]) -> None:
        async with self._lock:
            self._data[session_id].extend(entries)

    async def replace(self, session_id: str, entries: list[TranscriptEntry]) -> None:
        async with self._lock:
            self._data[session_id] = list(entries)

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            self._data.pop(session_id, None)


_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT (julianday('now'))
);
CREATE INDEX IF NOT EXISTS idx_session_entries_sid
    ON session_entries(session_id, id);
"""


class SQLiteSession(SQLiteStore):
    """A :class:`~lovia.session.Session` persisted to a SQLite file."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, _SESSION_SCHEMA)

    async def load(self, session_id: str) -> list[TranscriptEntry]:
        def _impl() -> list[TranscriptEntry]:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT payload FROM session_entries WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
                return [entry_from_dict(json.loads(r[0])) for r in rows]
            finally:
                self._release(conn)

        return await self._run(_impl)

    async def append(self, session_id: str, entries: list[TranscriptEntry]) -> None:
        def _impl() -> None:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT INTO session_entries (session_id, payload) VALUES (?, ?)",
                    [(session_id, json.dumps(entry_to_dict(it))) for it in entries],
                )
                conn.commit()
            finally:
                self._release(conn)

        await self._run(_impl)

    async def clear(self, session_id: str) -> None:
        def _impl() -> None:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM session_entries WHERE session_id = ?", (session_id,)
                )
                conn.commit()
            finally:
                self._release(conn)

        await self._run(_impl)

    async def replace(self, session_id: str, entries: list[TranscriptEntry]) -> None:
        def _impl() -> None:
            conn = self._connect()
            try:
                try:
                    conn.execute("BEGIN")
                    conn.execute(
                        "DELETE FROM session_entries WHERE session_id = ?",
                        (session_id,),
                    )
                    if entries:
                        conn.executemany(
                            "INSERT INTO session_entries (session_id, payload) VALUES (?, ?)",
                            [
                                (session_id, json.dumps(entry_to_dict(it)))
                                for it in entries
                            ],
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            finally:
                self._release(conn)

        await self._run(_impl)
