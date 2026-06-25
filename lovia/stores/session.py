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
import copy
import json
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

from ..session import Segment, Session
from ..types import JsonObject
from ..transcript import TranscriptEntry, entry_from_dict, entry_to_dict
from ._sqlite import SQLiteStore


class InMemorySession(Session):
    """A :class:`~lovia.session.Session` that keeps per-run segments in a dict."""

    def __init__(self) -> None:
        self._segments: dict[str, list[Segment]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def segments(self, session_id: str) -> list[Segment]:
        async with self._lock:
            # Copy entries (list) and meta (deep) so a caller mutating the
            # returned segments can't corrupt stored state through the read API
            # — matching the snapshot semantics SQLiteSession gets via JSON.
            return [
                Segment(seg.run_id, list(seg.entries), copy.deepcopy(seg.meta))
                for seg in self._segments.get(session_id, [])
            ]

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
            # Snapshot meta on write so a caller mutating/reusing the dict after
            # append can't retroactively change stored state (append-only).
            segs.append(
                Segment(run_id=rid, entries=list(entries), meta=copy.deepcopy(meta))
            )
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


class SQLiteSession(SQLiteStore, Session):
    """A :class:`~lovia.session.Session` persisted to a SQLite file.

    One row per run in ``session_runs``, keyed by ``(session_id, run_id)``:
    append is a single ``INSERT OR IGNORE`` (idempotent per ``run_id``) and an
    old row is never rewritten. ``segments`` returns each run in insertion
    order (the autoincrement ``id``, since ``run_id`` is opaque); ``load``
    (inherited) flattens them.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, _SESSION_SCHEMA)

    async def segments(self, session_id: str) -> list[Segment]:
        def _impl() -> list[Segment]:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT run_id, entries_json, meta_json FROM session_runs "
                    "WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
                return [
                    Segment(
                        run_id=r[0],
                        entries=[entry_from_dict(d) for d in json.loads(r[1])],
                        meta=json.loads(r[2]) if r[2] is not None else None,
                    )
                    for r in rows
                ]
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
