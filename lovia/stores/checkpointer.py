"""Checkpointer implementations: in-memory and SQLite-backed.

:class:`InMemoryCheckpointer` is a trivial in-process store suitable for
tests and short-lived runs. :class:`SQLiteCheckpointer` persists snapshots to
a SQLite file using the stdlib :mod:`sqlite3` driver via
:func:`asyncio.to_thread`; one row per ``run_id``, updates overwrite.
"""

from __future__ import annotations

from pathlib import Path

from ..checkpointer import RunSnapshot
from ._sqlite import SQLiteStore


class InMemoryCheckpointer:
    """Trivial in-process checkpointer. Useful for tests and short-lived runs."""

    def __init__(self) -> None:
        self._snapshots: dict[str, RunSnapshot] = {}

    async def save(self, snapshot: RunSnapshot) -> None:
        self._snapshots[snapshot.run_id] = snapshot

    async def load(self, run_id: str) -> RunSnapshot | None:
        return self._snapshots.get(run_id)

    async def delete(self, run_id: str) -> None:
        self._snapshots.pop(run_id, None)


_CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_snapshots (
    run_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class SQLiteCheckpointer(SQLiteStore):
    """Persist :class:`~lovia.checkpointer.RunSnapshot` instances to a SQLite database."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, _CHECKPOINT_SCHEMA)

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
            self._release(conn)

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
            self._release(conn)

    async def delete(self, run_id: str) -> None:
        await self._run(lambda: self._delete_sync(run_id))

    def _delete_sync(self, run_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM run_snapshots WHERE run_id = ?", (run_id,))
            conn.commit()
        finally:
            self._release(conn)
