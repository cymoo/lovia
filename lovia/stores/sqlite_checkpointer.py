"""SQLite-backed :class:`Checkpointer`.

One snapshot per ``run_id``; updates overwrite. Uses the stdlib :mod:`sqlite3`
driver through :func:`asyncio.to_thread` to avoid adding a dependency.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from ..checkpointer import RunSnapshot


_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_snapshots (
    run_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class SQLiteCheckpointer:
    """Persist :class:`RunSnapshot` instances to a SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not self._initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            self._initialized = True
        return conn

    async def save(self, snapshot: RunSnapshot) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync, snapshot)

    def _save_sync(self, snapshot: RunSnapshot) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO run_snapshots(run_id, payload, updated_at) "
                "VALUES (?, ?, ?)",
                (snapshot.run_id, snapshot.to_json(), snapshot.updated_at),
            )
            conn.commit()
        finally:
            conn.close()

    async def load(self, run_id: str) -> RunSnapshot | None:
        async with self._lock:
            return await asyncio.to_thread(self._load_sync, run_id)

    def _load_sync(self, run_id: str) -> RunSnapshot | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload FROM run_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            return RunSnapshot.from_json(row["payload"])
        finally:
            conn.close()

    async def delete(self, run_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, run_id)

    def _delete_sync(self, run_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM run_snapshots WHERE run_id = ?", (run_id,))
            conn.commit()
        finally:
            conn.close()
