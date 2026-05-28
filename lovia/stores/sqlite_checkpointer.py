"""SQLite-backed :class:`Checkpointer`.

One snapshot per ``run_id``; updates overwrite. Uses the stdlib :mod:`sqlite3`
driver through :func:`asyncio.to_thread` to avoid adding a dependency.
"""

from __future__ import annotations

from pathlib import Path

from ..checkpointer import RunSnapshot
from ._sqlite import SQLiteStore


_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_snapshots (
    run_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class SQLiteCheckpointer(SQLiteStore):
    """Persist :class:`RunSnapshot` instances to a SQLite database."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, _SCHEMA)

    async def save(self, snapshot: RunSnapshot) -> None:
        await self._run(lambda: self._save_sync(snapshot))

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
        return await self._run(lambda: self._load_sync(run_id))

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
        await self._run(lambda: self._delete_sync(run_id))

    def _delete_sync(self, run_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM run_snapshots WHERE run_id = ?", (run_id,))
            conn.commit()
        finally:
            conn.close()
