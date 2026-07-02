"""Session implementations: in-memory and SQLite-backed.

Both are **append-only run logs**: each finished run appends its entries as one
segment keyed by ``run_id``, and :meth:`load` returns the flat concatenation in
run order. Append is idempotent per ``run_id`` (re-issuing a completed run never
duplicates it). :class:`SQLiteSession` stores one row per run (never rewriting an
old row); :class:`InMemorySession` keeps the segments in a list. No extra
dependencies — SQLite goes through the stdlib :mod:`sqlite3` driver and
:func:`asyncio.to_thread`.

Both also provide ``trim_tool_results`` — an explicit **maintenance** operation
(not part of the :class:`~lovia.session.Session` protocol) that truncates old
stored tool outputs in place. It is the one sanctioned carve-out from
append-only: the runner never rewrites history, but an operator may reclaim
space, and the operation preserves what everything else depends on — run
boundaries, entry count and order, ``call_id`` pairing — so body indices,
summary coverage, and the (result-length-blind) compaction fingerprint all
survive. Configure a :class:`~lovia.context.FileResultStore` on the compaction
policy *before* relying on trim: offloaded outputs archived there stay fully
recoverable via ``recall_tool_result``; un-archived ones are truncated for good.
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
from ..transcript import (
    ToolResultEntry,
    TranscriptEntry,
    entry_from_dict,
    entry_to_dict,
)
from ._sqlite import SQLiteStore


def _trim_marker(call_id: str, dropped: int) -> str:
    """Honest tail appended to a truncated stored tool output."""
    return (
        f"\n[... {dropped:,} chars trimmed from stored history; "
        f'recall_tool_result("{call_id}") returns the full output '
        "only if it was archived to a result store]"
    )


# Fixed tail of the marker, used to recognize already-trimmed outputs so a
# periodic trim job is idempotent instead of shaving the marker itself.
_TRIM_SENTINEL = "archived to a result store]"


def _trim_entries(
    entries: list[TranscriptEntry], keep_chars: int
) -> tuple[list[TranscriptEntry], int]:
    """Truncate oversized tool outputs; return (new entries, trimmed count).

    Entries are replaced, never mutated — transcript entries are immutable by
    convention (identity-keyed token memos rely on it). Structure is preserved
    exactly: same entry count, order, ``call_id`` and ``is_error``; ``raw`` is
    dropped alongside the output it mirrors. A trim that would not actually
    shrink the output (the marker outweighs the saving) is skipped.
    """
    out: list[TranscriptEntry] = []
    trimmed = 0
    for entry in entries:
        if isinstance(entry, ToolResultEntry) and not entry.output.endswith(
            _TRIM_SENTINEL
        ):
            dropped = len(entry.output) - keep_chars
            marker = _trim_marker(entry.call_id, dropped)
            if dropped > len(marker):
                out.append(
                    ToolResultEntry(
                        call_id=entry.call_id,
                        output=entry.output[:keep_chars] + marker,
                        raw=None,
                        is_error=entry.is_error,
                    )
                )
                trimmed += 1
                continue
        out.append(entry)
    return out, trimmed


def _validate_trim_args(keep_chars: int, keep_runs: int) -> None:
    if keep_chars < 0:
        raise ValueError("keep_chars must be >= 0")
    if keep_runs < 0:
        raise ValueError("keep_runs must be >= 0")


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

    async def trim_tool_results(
        self, session_id: str, *, keep_chars: int = 400, keep_runs: int = 1
    ) -> int:
        """Truncate stored tool outputs older than the last ``keep_runs`` runs.

        A maintenance operation for long-lived sessions (see the module
        docstring for the contract): each qualifying
        :class:`~lovia.transcript.ToolResultEntry` keeps its first
        ``keep_chars`` characters plus an honest trim marker; entry structure
        is preserved. The ``keep_runs`` most recent runs stay verbatim — they
        are what the next run actually converses over. Idempotent. Returns
        the number of results trimmed.
        """
        _validate_trim_args(keep_chars, keep_runs)
        async with self._lock:
            segs = self._segments.get(session_id, [])
            total = 0
            for seg in segs[: max(0, len(segs) - keep_runs)]:
                entries, trimmed = _trim_entries(seg.entries, keep_chars)
                if trimmed:
                    seg.entries = entries
                    total += trimmed
            return total


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

    async def trim_tool_results(
        self, session_id: str, *, keep_chars: int = 400, keep_runs: int = 1
    ) -> int:
        """Truncate stored tool outputs older than the last ``keep_runs`` runs.

        Same contract as :meth:`InMemorySession.trim_tool_results`; rewrites
        only the rows whose entries actually changed, in one transaction.
        """
        _validate_trim_args(keep_chars, keep_runs)

        def _impl() -> int:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, entries_json FROM session_runs "
                    "WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
                total = 0
                for row_id, entries_json in rows[: max(0, len(rows) - keep_runs)]:
                    entries = [entry_from_dict(d) for d in json.loads(entries_json)]
                    trimmed_entries, trimmed = _trim_entries(entries, keep_chars)
                    if trimmed:
                        conn.execute(
                            "UPDATE session_runs SET entries_json = ? WHERE id = ?",
                            (
                                json.dumps([entry_to_dict(e) for e in trimmed_entries]),
                                row_id,
                            ),
                        )
                        total += trimmed
                conn.commit()
                return total
            except BaseException:
                # The ":memory:" handle is shared and outlives this call: a
                # partial trim left uncommitted here would be silently
                # committed by the next operation's commit(). (File-backed
                # connections roll back on close, but be explicit for both.)
                conn.rollback()
                raise
            finally:
                self._release(conn)

        return await self._run(_impl)
