"""Session implementations: in-memory and SQLite-backed.

Both are **append-only run logs**: each finished run appends its entries as one
segment keyed by ``run_id``, and :meth:`load` returns the flat concatenation in
run order. Append is idempotent per ``run_id`` (re-issuing a completed run never
duplicates it). :class:`SQLiteSession` stores one row per run (never rewriting an
old row); :class:`InMemorySession` keeps the segments in a list. No extra
dependencies — SQLite goes through the stdlib :mod:`sqlite3` driver and
:func:`asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..types import JsonObject
from ..transcript import TranscriptEntry, entry_from_dict, entry_to_dict
from ._sqlite import SQLiteStore


@dataclass
class _Segment:
    """One run's entries plus opaque per-run metadata, keyed by ``run_id``."""

    run_id: str
    entries: list[TranscriptEntry]
    meta: JsonObject | None = None


class InMemorySession:
    """A :class:`~lovia.session.Session` that keeps per-run segments in a dict."""

    def __init__(self) -> None:
        self._segments: dict[str, list[_Segment]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def load(self, session_id: str) -> list[TranscriptEntry]:
        async with self._lock:
            out: list[TranscriptEntry] = []
            for seg in self._segments.get(session_id, []):
                out.extend(seg.entries)
            return out

    async def append(
        self,
        session_id: str,
        entries: list[TranscriptEntry],
        *,
        run_id: str | None = None,
        meta: JsonObject | None = None,
    ) -> str:
        async with self._lock:
            segs = self._segments[session_id]
            rid = run_id if run_id is not None else uuid4().hex
            if any(seg.run_id == rid for seg in segs):
                return rid  # idempotent: this run is already stored
            segs.append(_Segment(run_id=rid, entries=list(entries), meta=meta))
            return rid

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            self._segments.pop(session_id, None)


_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    meta_json TEXT,
    created_at REAL NOT NULL DEFAULT (julianday('now')),
    UNIQUE(session_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_session_runs_sid
    ON session_runs(session_id, id);
"""


class SQLiteSession(SQLiteStore):
    """A :class:`~lovia.session.Session` persisted to a SQLite file.

    One row per run in ``session_runs``, keyed by ``(session_id, run_id)``:
    append is a single ``INSERT OR IGNORE`` (idempotent per ``run_id``) and an
    old row is never rewritten. ``load`` concatenates each run's entries in
    insertion order (the autoincrement ``id``, since ``run_id`` is opaque).
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, _SESSION_SCHEMA)

    async def load(self, session_id: str) -> list[TranscriptEntry]:
        def _impl() -> list[TranscriptEntry]:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT entries_json FROM session_runs "
                    "WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
                out: list[TranscriptEntry] = []
                for r in rows:
                    out.extend(entry_from_dict(d) for d in json.loads(r[0]))
                return out
            finally:
                self._release(conn)

        return await self._run(_impl)

    async def append(
        self,
        session_id: str,
        entries: list[TranscriptEntry],
        *,
        run_id: str | None = None,
        meta: JsonObject | None = None,
    ) -> str:
        rid = run_id if run_id is not None else uuid4().hex

        def _impl() -> None:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO session_runs "
                    "(session_id, run_id, entries_json, meta_json) VALUES (?, ?, ?, ?)",
                    (
                        session_id,
                        rid,
                        json.dumps([entry_to_dict(e) for e in entries]),
                        json.dumps(meta) if meta is not None else None,
                    ),
                )
                conn.commit()
            finally:
                self._release(conn)

        await self._run(_impl)
        return rid

    async def clear(self, session_id: str) -> None:
        def _impl() -> None:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM session_runs WHERE session_id = ?", (session_id,)
                )
                conn.commit()
            finally:
                self._release(conn)

        await self._run(_impl)
