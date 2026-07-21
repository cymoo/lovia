"""Chat store: a Session impl + a metadata table for the web UI.

The :class:`Session` Protocol only knows about ``load/append/clear`` for
transcript entries — it has no concept of "list all my chats" or "what's the
title of this one". The web layer needs both, so we add a *parallel*
metadata table (``chat_sessions``) alongside whatever ``Session`` backend
is used for transcript storage.

Defaults to a SQLite file. Pass any other ``Session`` impl
(e.g. :class:`InMemorySession`) to keep transcripts elsewhere — only the
metadata table is owned by this module.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from ..checkpointer import Checkpointer
from ..types import JsonObject
from ..session import Session
from ..stores import (
    InMemoryCheckpointer,
    InMemorySession,
    SQLiteCheckpointer,
    SQLiteSession,
)
from ..stores._sqlite import SQLiteStore

__all__ = ["ChatMeta", "ChatStore", "RunRow", "ScheduleRow"]

_T = TypeVar("_T")

# Column order shared by every ``ChatMeta`` SELECT (and ``ChatMeta.from_row``).
_META_COLS = "id, title, agent, created_at, updated_at, pinned"

# Column order shared by every ``ScheduleRow`` SELECT (and ``from_row``).
_SCHED_COLS = (
    "id, agent, input, session_id, trigger_kind, trigger_expr, "
    "next_fire, active, last_session_id, created_at, updated_at"
)

# Column order shared by every ``RunRow`` SELECT (and ``from_row``).
_RUN_COLS = (
    "id, session_id, agent, source, status, error, "
    "started_at, finished_at, usage_json"
)


_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    agent TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    active_run_id TEXT,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated
    ON chat_sessions(updated_at DESC);
CREATE TABLE IF NOT EXISTS chat_schedules (
    id TEXT PRIMARY KEY,
    agent TEXT,
    input TEXT NOT NULL,
    session_id TEXT,
    trigger_kind TEXT NOT NULL,
    trigger_expr TEXT NOT NULL,
    next_fire REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    last_session_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_schedules_due
    ON chat_schedules(active, next_fire);
CREATE TABLE IF NOT EXISTS chat_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    agent TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    usage_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_runs_session
    ON chat_runs(session_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_runs_source
    ON chat_runs(source, started_at DESC);
"""


@dataclass(frozen=True)
class ChatMeta:
    """One row of the chat metadata table."""

    id: str
    title: str | None
    agent: str | None
    created_at: float
    updated_at: float
    pinned: bool = False

    @classmethod
    def from_row(cls, row: Any) -> "ChatMeta":
        """Build from a ``_META_COLS`` row."""
        return cls(row[0], row[1], row[2], row[3], row[4], bool(row[5]))

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "title": self.title,
            "agent": self.agent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pinned": self.pinned,
        }


@dataclass(frozen=True)
class ScheduleRow:
    """One row of the ``chat_schedules`` table (a scheduled background run)."""

    id: str
    agent: str | None
    input: str
    session_id: str | None  # NULL → a fresh session per fire
    trigger_kind: str  # "cron" | "every" | "at"
    trigger_expr: str  # cron string | interval seconds | epoch timestamp
    next_fire: float
    active: bool
    last_session_id: str | None  # session of the last fire (overlap check)
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: Any) -> "ScheduleRow":
        return cls(
            id=row[0],
            agent=row[1],
            input=row[2],
            session_id=row[3],
            trigger_kind=row[4],
            trigger_expr=row[5],
            next_fire=row[6],
            active=bool(row[7]),
            last_session_id=row[8],
            created_at=row[9],
            updated_at=row[10],
        )

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "agent": self.agent,
            "input": self.input,
            "session_id": self.session_id,
            "trigger_kind": self.trigger_kind,
            "trigger_expr": self.trigger_expr,
            "next_fire": self.next_fire,
            "active": self.active,
            "last_session_id": self.last_session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class RunRow:
    """One row of the ``chat_runs`` table — the durable record of a
    supervised run (its *outcome*; the transcript itself lives in the session
    store, e.g. ``session_runs``).

    ``id`` is the run's checkpoint run_id when checkpointing is on (so a
    resumed run finalizes the same row), else a minted uuid. ``source`` names
    what started the run: ``"user"`` or ``"schedule:<id>"`` — a plain string,
    so new starters need no schema change. ``status`` extends the core
    :data:`~lovia.checkpointer.RunStatus` vocabulary with ``"cancelled"``
    (a user-requested stop, distinct from a failure).
    """

    id: str
    session_id: str | None  # NULL → the run never reached a session (bad fire)
    agent: str | None
    source: str
    status: str  # "running" | "completed" | "failed" | "cancelled" | "interrupted"
    error: str | None
    started_at: float
    finished_at: float | None
    # The sse.usage_dict shape: token totals, cache counts, and (when known)
    # last_input_tokens — the final call's prompt size.
    usage: JsonObject | None

    @classmethod
    def from_row(cls, row: Any) -> "RunRow":
        return cls(
            id=row[0],
            session_id=row[1],
            agent=row[2],
            source=row[3],
            status=row[4],
            error=row[5],
            started_at=row[6],
            finished_at=row[7],
            usage=json.loads(row[8]) if row[8] else None,
        )

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "agent": self.agent,
            "source": self.source,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "usage": self.usage,
        }


class ChatStore:
    """Session transcripts + chat metadata.

    Construction:

    * ``ChatStore.sqlite("./lovia.db")`` — persistent, single file (both
      transcripts and metadata in one DB).
    * ``ChatStore(InMemorySession(), meta_path=":memory:")`` — tests.
    * ``ChatStore(my_session, meta_path="…")`` — keep your custom session
      backend; we still get metadata.
    """

    def __init__(
        self,
        session: Session,
        *,
        meta_path: str | Path,
        checkpointer: Checkpointer | None = None,
        wal: bool = False,
    ) -> None:
        self.session = session
        # ``wal`` covers only the metadata store owned here; a caller-supplied
        # Session/Checkpointer configures its own (see ChatStore.sqlite, which
        # sets all three consistently).
        self._meta = SQLiteStore(str(meta_path), _META_SCHEMA, wal=wal)
        self.checkpointer: Checkpointer | None = checkpointer
        self._migrate()

    def _migrate(self) -> None:
        """Apply schema additions made after the initial release (idempotent).

        ``SQLiteStore`` ensures the base schema on first connect, but ``CREATE
        TABLE IF NOT EXISTS`` never adds a column to a table that already
        exists — so a column added later needs a guarded ``ALTER TABLE`` for
        pre-existing databases. The ``pinned`` index lives here (not in
        ``_META_SCHEMA``) because that script also runs against legacy DBs
        before this migration adds the column.
        """

        def add_column(conn: sqlite3.Connection, table: str, column_def: str) -> None:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
            except sqlite3.OperationalError as exc:
                # Another worker added the column between our PRAGMA check and
                # this ALTER (concurrent multi-worker startup). Tolerate that
                # one case; re-raise anything else.
                if "duplicate column" not in str(exc).lower():
                    raise

        with self._meta._tx() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)")}
            if "pinned" not in cols:
                add_column(conn, "chat_sessions", "pinned INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_sessions_pinned "
                "ON chat_sessions(pinned DESC, updated_at DESC)"
            )
            # DBs from before the chat_ prefix (lovia <= 0.8.26) name the
            # schedule table ``schedules``. The base schema (already ensured)
            # created the empty ``chat_schedules``; fold the legacy rows in and
            # drop the old table. Naming _SCHED_COLS explicitly also sheds the
            # dead ``last_status``/``last_error`` columns some legacy DBs carry
            # (retired by the run-records table) — they vanish with the DROP.
            legacy = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='schedules'"
            ).fetchone()
            if legacy is not None:
                try:
                    conn.execute(
                        f"INSERT OR IGNORE INTO chat_schedules ({_SCHED_COLS}) "
                        f"SELECT {_SCHED_COLS} FROM schedules"
                    )
                except sqlite3.OperationalError as exc:
                    # Another worker folded and dropped the table between our
                    # check and this read (concurrent multi-worker startup) —
                    # same tolerance as add_column above.
                    if "no such table" not in str(exc).lower():
                        raise
                conn.execute("DROP TABLE IF EXISTS schedules")

    # ---- low-level helpers ----------------------------------------------
    # One transaction/read dance, shared by every metadata method.

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        def _impl() -> None:
            with self._meta._tx() as conn:
                conn.execute(sql, params)

        await self._meta._run(_impl)

    async def _read_one(
        self, sql: str, params: tuple[Any, ...], map_row: Callable[[Any], _T]
    ) -> _T | None:
        def _impl() -> _T | None:
            with self._meta._conn() as conn:
                row = conn.execute(sql, params).fetchone()
                return map_row(row) if row is not None else None

        return await self._meta._run(_impl)

    async def _read_all(
        self, sql: str, params: tuple[Any, ...], map_row: Callable[[Any], _T]
    ) -> list[_T]:
        def _impl() -> list[_T]:
            with self._meta._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
                return [map_row(r) for r in rows]

        return await self._meta._run(_impl)

    # ---- factories ------------------------------------------------------

    @classmethod
    def sqlite(cls, path: str | Path, *, wal: bool = False) -> "ChatStore":
        """Persistent store: transcripts, metadata, and checkpoints in one file.

        Three stores share that file; pass ``wal=True`` when running multiple
        workers (or to let readers proceed during writes) — it enables WAL
        journal mode and a busy timeout on all three.
        """
        # The default path nests under ./.lovia, and sqlite3.connect cannot
        # create parent directories.
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return cls(
            SQLiteSession(path, wal=wal),
            meta_path=path,
            checkpointer=SQLiteCheckpointer(path, wal=wal),
            wal=wal,
        )

    @classmethod
    def in_memory(cls) -> "ChatStore":
        """Volatile store for tests and one-off demos."""
        return cls(
            InMemorySession(),
            meta_path=":memory:",
            checkpointer=InMemoryCheckpointer(),
        )

    # ---- metadata -------------------------------------------------------

    async def upsert(
        self,
        session_id: str,
        *,
        agent: str | None = None,
        title: str | None = None,
    ) -> None:
        """Insert a row if missing, otherwise bump ``updated_at``.

        ``title`` is applied only on insert (a provisional title for a brand-new
        session); on conflict the existing title is left untouched so a
        background-generated title is never clobbered.
        """
        now = time.time()
        await self._write(
            """
            INSERT INTO chat_sessions (id, title, agent, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at = excluded.updated_at,
                agent = COALESCE(chat_sessions.agent, excluded.agent)
            """,
            (session_id, (title.strip()[:120] if title else None), agent, now, now),
        )

    async def set_title(self, session_id: str, title: str) -> None:
        title = title.strip()[:120]
        await self._write(
            "UPDATE chat_sessions SET title = ? WHERE id = ?", (title, session_id)
        )

    async def set_pinned(self, session_id: str, pinned: bool) -> None:
        """Pin or unpin a session (pinned sessions sort to the top)."""
        await self._write(
            "UPDATE chat_sessions SET pinned = ? WHERE id = ?",
            (1 if pinned else 0, session_id),
        )

    async def set_title_if_unchanged(
        self, session_id: str, title: str, *, expected: str | None
    ) -> None:
        """Set the title only if it still equals ``expected``.

        Used by the background title task: if the user renamed the chat in the
        meantime, ``expected`` (the provisional title) no longer matches and the
        generated title is dropped rather than clobbering the user's choice.
        """
        title = title.strip()[:120]
        await self._write(
            "UPDATE chat_sessions SET title = ? WHERE id = ? AND title IS ?",
            (title, session_id, expected),
        )

    async def get(self, session_id: str) -> ChatMeta | None:
        return await self._read_one(
            f"SELECT {_META_COLS} FROM chat_sessions WHERE id = ?",
            (session_id,),
            ChatMeta.from_row,
        )

    # ``Sequence`` (not ``list[...]``) on the read methods: this method shadows
    # the ``list`` builtin inside the class body, so a later ``list[ChatMeta]``
    # annotation would resolve to the method and fail strict mypy. Matches the
    # schedule reads (``list_schedules``/``due_schedules``) anyway.
    async def list(self, *, limit: int = 200, offset: int = 0) -> Sequence[ChatMeta]:
        """Return chat metadata, pinned first, then most recent activity."""
        return await self._read_all(
            f"SELECT {_META_COLS} FROM chat_sessions "
            "ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
            ChatMeta.from_row,
        )

    async def delete(self, session_id: str) -> None:
        """Remove transcript, checkpoint, AND metadata for ``session_id``.

        The checkpoint is dropped first: once the metadata row is gone its
        ``active_run_id`` is unreadable, so an interrupted run's snapshot would
        otherwise be stranded (unreachable, never resumable, never cleaned up).
        """
        await self._drop_checkpoint(session_id)
        await self.session.clear(session_id)
        await self._write("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        await self._write(
            "DELETE FROM chat_runs WHERE session_id = ?", (session_id,)
        )

    async def delete_all(self) -> None:
        """Remove ALL transcripts, checkpoints, and metadata."""
        # Read every id directly — ``list`` caps at its limit, which would
        # leave the transcripts/checkpoints of sessions beyond one page orphaned
        # while the unconditional row delete below wiped their metadata.
        ids = await self._read_all(
            "SELECT id FROM chat_sessions", (), lambda row: row[0]
        )
        for session_id in ids:
            await self._drop_checkpoint(session_id)
            await self.session.clear(session_id)
        await self._write("DELETE FROM chat_sessions")
        await self._write("DELETE FROM chat_runs")

    async def _drop_checkpoint(self, session_id: str) -> None:
        """Delete the session's active-run snapshot, if any (best-effort)."""
        if self.checkpointer is None:
            return
        run_id = await self.get_active_run_id(session_id)
        if run_id:
            await self.checkpointer.delete(run_id)

    async def search(
        self, query: str, *, limit: int = 200, offset: int = 0
    ) -> Sequence[ChatMeta]:
        """Search sessions whose title or id contains ``query`` (literally —
        LIKE wildcards in the query are escaped, so "100%" matches "100%")."""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        return await self._read_all(
            f"SELECT {_META_COLS} FROM chat_sessions "
            "WHERE title LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\' "
            "ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?",
            (pattern, pattern, limit, offset),
            ChatMeta.from_row,
        )

    # ---- active run tracking --------------------------------------------

    async def get_active_run_id(self, session_id: str) -> str | None:
        """Return the run_id of the most recent unfinished run, or None."""
        return await self._read_one(
            "SELECT active_run_id FROM chat_sessions WHERE id = ?",
            (session_id,),
            lambda row: row[0],
        )

    async def set_active_run_id(self, session_id: str, run_id: str) -> None:
        """Record that ``run_id`` is the active (potentially interrupted) run."""
        await self._write(
            "UPDATE chat_sessions SET active_run_id = ? WHERE id = ?",
            (run_id, session_id),
        )

    async def clear_active_run_id(
        self, session_id: str, *, expected: str | None = None
    ) -> None:
        """Clear the active run pointer (run completed or was abandoned).

        With ``expected`` set, only clears when the stored pointer still names
        that run — so a finished run doesn't wipe a pointer a newer run claimed.
        """
        if expected is None:
            await self._write(
                "UPDATE chat_sessions SET active_run_id = NULL WHERE id = ?",
                (session_id,),
            )
        else:
            await self._write(
                "UPDATE chat_sessions SET active_run_id = NULL "
                "WHERE id = ? AND active_run_id = ?",
                (session_id, expected),
            )

    # ---- schedules ------------------------------------------------------

    async def add_schedule(self, row: ScheduleRow) -> None:
        await self._write(
            f"INSERT INTO chat_schedules ({_SCHED_COLS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.id,
                row.agent,
                row.input,
                row.session_id,
                row.trigger_kind,
                row.trigger_expr,
                row.next_fire,
                int(row.active),
                row.last_session_id,
                row.created_at,
                row.updated_at,
            ),
        )

    async def list_schedules(self) -> Sequence[ScheduleRow]:
        return await self._read_all(
            f"SELECT {_SCHED_COLS} FROM chat_schedules ORDER BY created_at DESC",
            (),
            ScheduleRow.from_row,
        )

    async def get_schedule(self, schedule_id: str) -> ScheduleRow | None:
        return await self._read_one(
            f"SELECT {_SCHED_COLS} FROM chat_schedules WHERE id = ?",
            (schedule_id,),
            ScheduleRow.from_row,
        )

    async def update_schedule(self, row: ScheduleRow) -> None:
        """Overwrite every mutable column of the schedule (keyed by ``row.id``)."""
        await self._write(
            "UPDATE chat_schedules SET agent = ?, input = ?, session_id = ?, "
            "trigger_kind = ?, trigger_expr = ?, next_fire = ?, active = ?, "
            "last_session_id = ?, updated_at = ? WHERE id = ?",
            (
                row.agent,
                row.input,
                row.session_id,
                row.trigger_kind,
                row.trigger_expr,
                row.next_fire,
                int(row.active),
                row.last_session_id,
                row.updated_at,
                row.id,
            ),
        )

    async def delete_schedule(self, schedule_id: str) -> bool:
        """Delete a schedule (and its run history); returns whether it existed."""
        existed = (await self.get_schedule(schedule_id)) is not None
        await self._write("DELETE FROM chat_schedules WHERE id = ?", (schedule_id,))
        await self._write(
            "DELETE FROM chat_runs WHERE source = ?", (f"schedule:{schedule_id}",)
        )
        return existed

    async def due_schedules(self, now: float) -> Sequence[ScheduleRow]:
        """Active schedules whose ``next_fire`` is at or before ``now``."""
        return await self._read_all(
            f"SELECT {_SCHED_COLS} FROM chat_schedules "
            "WHERE active = 1 AND next_fire <= ? ORDER BY next_fire",
            (now,),
            ScheduleRow.from_row,
        )

    async def mark_fired(
        self,
        schedule_id: str,
        *,
        next_fire: float,
        active: bool,
        last_session_id: str | None,
    ) -> None:
        """Advance a schedule after a fire (or deactivate a one-shot)."""
        await self._write(
            "UPDATE chat_schedules SET next_fire = ?, active = ?, "
            "last_session_id = ?, updated_at = ? WHERE id = ?",
            (next_fire, int(active), last_session_id, time.time(), schedule_id),
        )

    async def set_schedule_active(
        self, schedule_id: str, *, active: bool, next_fire: float | None = None
    ) -> None:
        """Pause/resume a schedule (resume passes a freshly-computed next_fire)."""
        if next_fire is None:
            await self._write(
                "UPDATE chat_schedules SET active = ?, updated_at = ? WHERE id = ?",
                (int(active), time.time(), schedule_id),
            )
        else:
            await self._write(
                "UPDATE chat_schedules SET active = ?, next_fire = ?, updated_at = ? "
                "WHERE id = ?",
                (int(active), next_fire, time.time(), schedule_id),
            )

    # ---- run records -----------------------------------------------------
    # The durable side of run supervision: the supervisor inserts a row when a
    # run starts and finalizes it on wind-down, so "did that run succeed?"
    # survives eviction and restarts (schedule history, missed-completion
    # notices, a future runs view).

    async def start_run(self, row: RunRow) -> None:
        """Insert a run record.

        Re-inserting an existing id — resuming an interrupted run — flips the
        row back to the new status while keeping the original ``started_at``
        and ``source``, so the whole run stays one record.
        """
        await self._write(
            f"INSERT INTO chat_runs ({_RUN_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status, "
            "error = excluded.error, finished_at = excluded.finished_at",
            (
                row.id,
                row.session_id,
                row.agent,
                row.source,
                row.status,
                row.error,
                row.started_at,
                row.finished_at,
                json.dumps(row.usage) if row.usage is not None else None,
            ),
        )

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Finalize a run record with its terminal status/error/usage."""
        await self._write(
            "UPDATE chat_runs SET status = ?, error = ?, finished_at = ?, "
            "usage_json = ? WHERE id = ?",
            (
                status,
                error,
                time.time(),
                json.dumps(usage) if usage is not None else None,
                run_id,
            ),
        )

    async def list_runs(
        self,
        *,
        session_id: str | None = None,
        source: str | None = None,
        since: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[RunRow]:
        """Run records, newest first, optionally filtered.

        ``since`` keeps only runs that finished after that timestamp — the
        "what completed while I was away?" query behind reload-surviving
        notifications.
        """
        where: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if since is not None:
            where.append("finished_at > ?")
            params.append(since)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        return await self._read_all(
            f"SELECT {_RUN_COLS} FROM chat_runs {clause}"
            "ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
            RunRow.from_row,
        )

    async def latest_run_for(self, source: str) -> RunRow | None:
        """The most recent run started by ``source`` (a schedule's last outcome)."""
        return await self._read_one(
            f"SELECT {_RUN_COLS} FROM chat_runs WHERE source = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (source,),
            RunRow.from_row,
        )

    async def sweep_stale_runs(self) -> None:
        """Settle rows a dead process left ``running`` as ``interrupted``.

        Call once at startup, before any run starts: live runs are per-process
        (the supervisor's), so at that point every ``running`` row is a run the
        previous process took down with it.
        """
        await self._write(
            "UPDATE chat_runs SET status = 'interrupted', finished_at = ? "
            "WHERE status = 'running'",
            (time.time(),),
        )
