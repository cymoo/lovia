"""Checkpointer implementations: in-memory and SQLite-backed.

Both store a run as an **append-only entry log plus a small mutable head**:
:meth:`append` adds one turn's entries and overwrites the head, so a long run
never rewrites the entries it already persisted. :class:`SQLiteCheckpointer`
keeps one row per turn (``snapshot_turns``) and one head row
(``snapshot_heads``); :class:`InMemoryCheckpointer` keeps them in dicts.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..transcript import TranscriptEntry, entry_from_dict, entry_to_dict
from ..checkpointer import RunHead, RunSnapshot
from ._sqlite import SQLiteStore


class InMemoryCheckpointer:
    """Trivial in-process checkpointer. Useful for tests and short-lived runs."""

    def __init__(self) -> None:
        self._entries: dict[str, list[TranscriptEntry]] = {}
        self._heads: dict[str, RunHead] = {}

    async def append(
        self, run_id: str, entries: list[TranscriptEntry], head: RunHead
    ) -> None:
        self._entries.setdefault(run_id, []).extend(entries)
        self._heads[run_id] = head

    async def load(self, run_id: str) -> RunSnapshot | None:
        head = self._heads.get(run_id)
        if head is None:
            return None
        return RunSnapshot.from_parts(run_id, list(self._entries.get(run_id, [])), head)

    async def delete(self, run_id: str) -> None:
        self._entries.pop(run_id, None)
        self._heads.pop(run_id, None)


_CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot_heads (
    run_id TEXT PRIMARY KEY,
    head_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_turns (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    entries_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


class SQLiteCheckpointer(SQLiteStore):
    """Persist a run to SQLite as append-only turn rows plus one head row."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, _CHECKPOINT_SCHEMA)

    async def append(
        self, run_id: str, entries: list[TranscriptEntry], head: RunHead
    ) -> None:
        await self._run(lambda: self._append_sync(run_id, entries, head))

    def _append_sync(
        self, run_id: str, entries: list[TranscriptEntry], head: RunHead
    ) -> None:
        conn = self._connect()
        try:
            if entries:
                next_seq = conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 FROM snapshot_turns "
                    "WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO snapshot_turns (run_id, seq, entries_json) "
                    "VALUES (?, ?, ?)",
                    (run_id, next_seq, json.dumps([entry_to_dict(e) for e in entries])),
                )
            conn.execute(
                "INSERT OR REPLACE INTO snapshot_heads (run_id, head_json, updated_at) "
                "VALUES (?, ?, ?)",
                (run_id, head.to_json(), head.updated_at),
            )
            conn.commit()
        finally:
            self._release(conn)

    async def load(self, run_id: str) -> RunSnapshot | None:
        return await self._run(lambda: self._load_sync(run_id))

    def _load_sync(self, run_id: str) -> RunSnapshot | None:
        conn = self._connect()
        try:
            head_row = conn.execute(
                "SELECT head_json FROM snapshot_heads WHERE run_id = ?", (run_id,)
            ).fetchone()
            if head_row is None:
                return None
            head = RunHead.from_json(head_row[0])
            rows = conn.execute(
                "SELECT entries_json FROM snapshot_turns WHERE run_id = ? "
                "ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
            entries: list[TranscriptEntry] = []
            for r in rows:
                entries.extend(entry_from_dict(d) for d in json.loads(r[0]))
            return RunSnapshot.from_parts(run_id, entries, head)
        finally:
            self._release(conn)

    async def delete(self, run_id: str) -> None:
        await self._run(lambda: self._delete_sync(run_id))

    def _delete_sync(self, run_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM snapshot_turns WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM snapshot_heads WHERE run_id = ?", (run_id,))
            conn.commit()
        finally:
            self._release(conn)
