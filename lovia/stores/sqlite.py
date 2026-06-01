"""SQLite-backed :class:`Session`.

Uses :mod:`sqlite3` from the stdlib via :func:`asyncio.to_thread` so we don't
add ``aiosqlite`` as a dependency. Concurrency is serialized through a single
async lock; that's plenty for the kind of workloads agent frameworks see.

The schema is intentionally trivial: entries are stored as JSON blobs in
insertion order (one row per :class:`TranscriptEntry`). Loading deserializes
them via :func:`lovia.transcript.entry_from_dict`.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..transcript import TranscriptEntry, entry_from_dict, entry_to_dict
from ._sqlite import SQLiteStore


_SCHEMA = """
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
    """A :class:`Session` persisted to a SQLite file."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, _SCHEMA)

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
                # Single transaction: delete existing rows, then insert the
                # new transcript. On error we rollback so the old transcript
                # survives intact.
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
