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
import secrets
import sqlite3
import time
import uuid
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from ..checkpointer import Checkpointer
from ..types import JsonObject
from ..session import Segment, Session
from ..stores import (
    InMemoryCheckpointer,
    InMemorySession,
    SQLiteCheckpointer,
    SQLiteSession,
)
from ..stores._sqlite import Migration, SQLiteStore
from ..stores.session import _rewind_split
from ..transcript import InputEntry

__all__ = ["ChatMeta", "ChatStore", "RunRow", "ScheduleRow"]

_T = TypeVar("_T")

# Column order shared by every ``ChatMeta`` SELECT (and ``ChatMeta.from_row``).
_META_COLS = (
    "id, title, agent, created_at, updated_at, pinned, parent_id, "
    "origin_kind, origin_session_id, origin_ref"
)

# Column order shared by every ``ScheduleRow`` SELECT (and ``from_row``).
_SCHED_COLS = (
    "id, agent, input, session_id, trigger_kind, trigger_expr, "
    "next_fire, active, last_session_id, created_at, updated_at, "
    "until, max_fires, expires_at, fire_count, finished_reason, context_json"
)


def _dumps_context(context: JsonObject | None) -> str | None:
    """A client context as stored; an empty one stores as NULL."""
    return json.dumps(context, ensure_ascii=False) if context else None


def _ref_offset(segments: list[Segment], ref: str) -> int | None:
    """The flat transcript index of the user message ``ref`` names, or None.

    Reads the ``"<run_id>:<n>"`` refs ``api.serialization.user_turn_refs``
    hands out: the ``n``-th user entry of run ``run_id``'s segment.
    """
    run_id, _, n = ref.rpartition(":")
    if not n.isdigit():
        return None
    offset = 0
    for seg in segments:
        if seg.run_id == run_id:
            users = [
                i
                for i, entry in enumerate(seg.entries)
                if isinstance(entry, InputEntry) and entry.role == "user"
            ]
            return offset + users[int(n)] if int(n) < len(users) else None
        offset += len(seg.entries)
    return None


# Column order shared by every ``RunRow`` SELECT (and ``from_row``).
_RUN_COLS = (
    "id, session_id, agent, source, status, error, started_at, finished_at, usage_json"
)


_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    agent TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    active_run_id TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    parent_id TEXT,
    active_context_json TEXT
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
    updated_at REAL NOT NULL,
    until TEXT,
    max_fires INTEGER,
    expires_at REAL,
    fire_count INTEGER NOT NULL DEFAULT 0,
    finished_reason TEXT,
    context_json TEXT
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
CREATE TABLE IF NOT EXISTS chat_ui_data (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_ui_data_session
    ON chat_ui_data(session_id);
"""

# Columns added after their table first shipped, before the chat tables were
# versioned, as (table, column definition). Frozen: ``_META_SCHEMA`` declares
# them all, and a later change is a step in ``_MIGRATIONS``.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("chat_sessions", "pinned INTEGER NOT NULL DEFAULT 0"),
    # Subagent child sessions (0.9.17): a task session points at the chat that
    # spawned it, so the UI can group tasks out of the list.
    ("chat_sessions", "parent_id TEXT"),
    # Schedule stop conditions (0.8.34).
    ("chat_schedules", "until TEXT"),
    ("chat_schedules", "max_fires INTEGER"),
    ("chat_schedules", "expires_at REAL"),
    ("chat_schedules", "fire_count INTEGER NOT NULL DEFAULT 0"),
    ("chat_schedules", "finished_reason TEXT"),
    # The page's client context for run_deps= (0.9.95): the in-flight run's
    # (beside its pointer, cleared with it) and a schedule's (for its fires).
    ("chat_sessions", "active_context_json TEXT"),
    ("chat_schedules", "context_json TEXT"),
)

# Indexes over an added column. They can't live in ``_META_SCHEMA``: that
# script also runs against an older database, before its columns exist.
_ADDED_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_pinned "
    "ON chat_sessions(pinned DESC, updated_at DESC)",
)


def _reconcile_unversioned(conn: sqlite3.Connection) -> None:
    """Version 1: bring a database from before versioning up to ``_META_SCHEMA``.

    Such a database may stop anywhere in :data:`_ADDED_COLUMNS`, so a column is
    added only where it is missing; a fresh database already has them all.
    """
    for table, column_def in _ADDED_COLUMNS:
        present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column_def.split(" ", 1)[0] not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
    for statement in _ADDED_INDEXES:
        conn.execute(statement)


def _fork_origin(conn: sqlite3.Connection) -> None:
    """Version 2: forks (#306).

    * ``origin_*`` — where a chat came from (a fork of another chat), kept
      apart from ``parent_id``: ownership cascades on delete, provenance never.
    * ``chat_ui_data`` keyed by ``(session_id, id)``, so a fork copies its
      source's rows verbatim and the refs in the copied transcript resolve.
    * The ``parent_id`` index that waited for a migration of its own (#299).
    """
    for column in ("origin_kind", "origin_session_id", "origin_ref"):
        conn.execute(f"ALTER TABLE chat_sessions ADD COLUMN {column} TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_sessions_origin "
        "ON chat_sessions(origin_session_id, origin_ref)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_sessions_parent "
        "ON chat_sessions(parent_id)"
    )
    # SQLite can't change a primary key in place: rebuild the table. The
    # composite key's leading column also serves per-session lookups, so the
    # old session index goes with the old table.
    conn.execute(
        "CREATE TABLE chat_ui_data_v2 ("
        "session_id TEXT NOT NULL, id TEXT NOT NULL, created_at REAL NOT NULL, "
        "payload BLOB NOT NULL, PRIMARY KEY (session_id, id))"
    )
    conn.execute(
        "INSERT INTO chat_ui_data_v2 (session_id, id, created_at, payload) "
        "SELECT session_id, id, created_at, payload FROM chat_ui_data"
    )
    conn.execute("DROP TABLE chat_ui_data")
    conn.execute("ALTER TABLE chat_ui_data_v2 RENAME TO chat_ui_data")


# Changes to the chat tables since ``_META_SCHEMA``, in order (see
# ``SQLiteStore``). Append a step; never edit one that has shipped.
_MIGRATIONS: tuple[Migration, ...] = (_reconcile_unversioned, _fork_origin)


@dataclass(frozen=True)
class ChatMeta:
    """One row of the chat metadata table."""

    id: str
    title: str | None
    agent: str | None
    created_at: float
    updated_at: float
    pinned: bool = False
    parent_id: str | None = None
    """Set on a subagent task session: the chat that spawned it. The UI
    groups such sessions under a Tasks section instead of the chat list."""
    origin_kind: str | None = None
    """Where the chat came from — ``"fork"`` — or None. Provenance only:
    unlike ``parent_id`` it never cascades a delete, and the source may be
    gone."""
    origin_session_id: str | None = None
    origin_ref: str | None = None
    """For a fork: the source's user message the fork was cut before (None
    for a whole-chat copy)."""

    @classmethod
    def from_row(cls, row: Any) -> "ChatMeta":
        """Build from a ``_META_COLS`` row."""
        return cls(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            bool(row[5]),
            row[6],
            row[7],
            row[8],
            row[9],
        )

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "title": self.title,
            "agent": self.agent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pinned": self.pinned,
            "parent_id": self.parent_id,
            "origin_kind": self.origin_kind,
            "origin_session_id": self.origin_session_id,
            "origin_ref": self.origin_ref,
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
    # Stop condition, evaluated by each fired run itself (see scheduler.fire_input).
    until: str | None = None
    # Safety nets, enforced deterministically by the scheduler. ``fire_count``
    # counts fire *attempts* (skipped slots included) — a bounded lifetime even
    # when fires never deliver, not an exact delivery quota.
    max_fires: int | None = None
    expires_at: float | None = None
    fire_count: int = 0
    # Why the scheduler/agent deactivated it; NULL on a plain user pause, so
    # ``active=0 AND finished_reason IS NOT NULL`` reads as "done".
    finished_reason: str | None = None
    # The client context its fires run with (create_app(run_deps=...)): the
    # page's, when the schedule was made — a schedule is a deferred message.
    context: JsonObject | None = None

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
            until=row[11],
            max_fires=row[12],
            expires_at=row[13],
            fire_count=row[14],
            finished_reason=row[15],
            context=json.loads(row[16]) if row[16] else None,
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
            "until": self.until,
            "max_fires": self.max_fires,
            "expires_at": self.expires_at,
            "fire_count": self.fire_count,
            "finished_reason": self.finished_reason,
            "context": self.context,
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
        self._meta = SQLiteStore(
            str(meta_path), _META_SCHEMA, name="chat", migrations=_MIGRATIONS, wal=wal
        )
        self.checkpointer: Checkpointer | None = checkpointer
        # Migrate now rather than on the first request, so a database this
        # lovia can't open fails the server at startup.
        with self._meta._conn():
            pass

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
    def sqlite(cls, path: str | Path, *, wal: bool = True) -> "ChatStore":
        """Persistent store: transcripts, metadata, and checkpoints in one file.

        Three stores share that file, each with its own asyncio lock, so only
        the database sees them collide — and they collide constantly: the
        checkpointer writes after every model message and every tool result,
        once per running agent, while the UI, the scheduler and the run
        supervisor read. WAL is therefore on by default here: a rollback
        journal makes those readers wait for the writer, which on the serving
        shape (five agents writing, one reader) measured ~6x the median read
        latency and a third of the read throughput.

        The mode is a property of the file, so an existing database migrates
        on first open. Its ``-wal`` and ``-shm`` companions exist only while a
        connection is open — every call here opens and closes its own, and the
        last close folds the WAL back in — so a stopped server still leaves one
        file. Pass ``wal=False`` for a filesystem that cannot host those two;
        SQLite also declines WAL on its own there, with a warning, so this is
        belt-and-braces rather than a requirement.
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
        parent_id: str | None = None,
    ) -> None:
        """Insert a row if missing, otherwise bump ``updated_at``.

        ``title`` is applied only on insert (a provisional title for a brand-new
        session); on conflict the existing title is left untouched so a
        background-generated title is never clobbered. ``parent_id`` likewise
        sticks from the insert — a session's task-ness never changes later.
        """
        now = time.time()
        await self._write(
            """
            INSERT INTO chat_sessions (id, title, agent, created_at, updated_at,
                                       parent_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at = excluded.updated_at,
                agent = COALESCE(chat_sessions.agent, excluded.agent)
            """,
            (
                session_id,
                (title.strip()[:120] if title else None),
                agent,
                now,
                now,
                parent_id,
            ),
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

    async def fork(self, source_id: str, *, ref: str | None = None) -> str | None:
        """Copy ``source_id``'s finished history into a new chat; return its id.

        With ``ref`` (a user message's ``"<run_id>:<n>"``) the copy stops just
        before that message; without, it is the whole chat. Returns None when
        that leaves nothing to copy. Raises ``ValueError`` when ``ref`` names
        no message of the finished history, and ``LookupError`` when the
        source chat is gone.

        The source is only read, and once: that ``segments`` call is the
        fork's snapshot, so a run, rewind or delete racing it needs no lock.
        The copy is published when complete rather than atomic — the
        transcript lands under a fresh id nobody holds yet, then one metadata
        transaction re-checks the source, copies its UI data and inserts the
        row that makes the chat exist. A failure clears the copied transcript;
        a crash in between leaves it unreferenced, where nothing shows it (the
        ``Session`` protocol can't enumerate ids, so there is no sweep).
        """
        segments = await self.session.segments(source_id)
        if ref is None:
            kept, boundary = segments, None
        else:
            keep = _ref_offset(segments, ref)
            if keep is None:
                raise ValueError(f"no message {ref!r} in the chat's finished history")
            # The same cut a rewind makes: whole runs keep their meta, the cut
            # one drops it (its carried context state describes the tail).
            kept, boundary, _ = _rewind_split(segments, keep)
        copied = [*kept, *([boundary] if boundary is not None else [])]
        if not copied:
            return None

        fork_id = uuid.uuid4().hex
        now = time.time()

        def _publish() -> bool:
            with self._meta._tx() as conn:
                source = conn.execute(
                    "SELECT title, agent FROM chat_sessions WHERE id = ?", (source_id,)
                ).fetchone()
                if source is None:
                    return False
                conn.execute(
                    "INSERT INTO chat_ui_data (session_id, id, created_at, payload) "
                    "SELECT ?, id, created_at, payload FROM chat_ui_data "
                    "WHERE session_id = ?",
                    (fork_id, source_id),
                )
                conn.execute(
                    "INSERT INTO chat_sessions (id, title, agent, created_at, "
                    "updated_at, origin_kind, origin_session_id, origin_ref) "
                    "VALUES (?, ?, ?, ?, ?, 'fork', ?, ?)",
                    (fork_id, source[0], source[1], now, now, source_id, ref),
                )
                return True

        try:
            for seg in copied:
                await self.session.append(
                    fork_id, seg.entries, run_id=seg.run_id, meta=seg.meta
                )
            published = await self._meta._run(_publish)
        except Exception:
            # Not on cancellation: the publish may still commit in its worker
            # thread, and clearing then would empty a chat that exists.
            await self.session.clear(fork_id)
            raise
        if not published:
            await self.session.clear(fork_id)
            raise LookupError(source_id)
        return fork_id

    # ``Sequence`` (not ``list[...]``) on the read methods: this method shadows
    # the ``list`` builtin inside the class body, so a later ``list[ChatMeta]``
    # annotation would resolve to the method and fail strict mypy. Matches the
    # schedule reads (``list_schedules``/``due_schedules``) anyway.
    async def list(self, *, limit: int = 200, offset: int = 0) -> Sequence[ChatMeta]:
        """Return chat metadata, pinned first, then most recent activity.

        Subagent task sessions (``parent_id`` set) are excluded — they belong
        to their parent chat (``list_children``), and letting them into the
        page would crowd real chats out of the sidebar's window.
        """
        return await self._read_all(
            f"SELECT {_META_COLS} FROM chat_sessions WHERE parent_id IS NULL "
            "ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
            ChatMeta.from_row,
        )

    async def delete(self, session_id: str, *, schedules: bool = True) -> None:
        """Remove transcript, checkpoint, AND metadata for ``session_id`` and
        its subagent task sessions — listed only under their chat, a task left
        behind would be unreachable. Schedules bound to them go too (with their
        run history): one would otherwise bring the chat back on its next fire.
        ``schedules=False`` keeps them — for undoing a chat a schedule's own
        fire just created.

        Checkpoints are dropped first: once a metadata row is gone its
        ``active_run_id`` is unreadable, so an interrupted run's snapshot would
        otherwise be stranded (unreachable, never resumable, never cleaned up).
        The metadata goes last, in one transaction, so a delete that fails part
        way leaves every chat in place for a retry. Every step deletes the same
        snapshot of the tree: a task added meanwhile is left whole, not stripped
        of its metadata alone.
        """
        ids = await self.task_tree(session_id)
        for sid in ids:
            await self._drop_checkpoint(sid)
            await self.session.clear(sid)

        def _impl() -> None:
            with self._meta._tx() as conn:
                bound = [
                    (row[0],)
                    for sid in (ids if schedules else ())
                    for row in conn.execute(
                        "SELECT id FROM chat_schedules WHERE session_id = ?", (sid,)
                    )
                ]
                conn.executemany(
                    "DELETE FROM chat_runs WHERE source = 'schedule:' || ?", bound
                )
                conn.executemany("DELETE FROM chat_schedules WHERE id = ?", bound)
                for table, column in (
                    ("chat_runs", "session_id"),
                    ("chat_ui_data", "session_id"),
                    ("chat_sessions", "id"),
                ):
                    conn.executemany(
                        f"DELETE FROM {table} WHERE {column} = ?",
                        [(sid,) for sid in ids],
                    )

        await self._meta._run(_impl)

    async def delete_all(self) -> None:
        """Remove ALL transcripts, checkpoints, and metadata — and the
        schedules bound to a chat (as ``delete``)."""
        # Read every id directly — ``list`` caps at its limit, which would
        # leave the transcripts/checkpoints of sessions beyond one page orphaned
        # while the unconditional row delete below wiped their metadata.
        ids = await self._read_all(
            "SELECT id FROM chat_sessions", (), lambda row: row[0]
        )
        for session_id in ids:
            await self._drop_checkpoint(session_id)
            await self.session.clear(session_id)
        await self._write("DELETE FROM chat_schedules WHERE session_id IS NOT NULL")
        await self._write("DELETE FROM chat_sessions")
        await self._write("DELETE FROM chat_runs")
        await self._write("DELETE FROM chat_ui_data")

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
            "WHERE parent_id IS NULL "
            "AND (title LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\') "
            "ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?",
            (pattern, pattern, limit, offset),
            ChatMeta.from_row,
        )

    # ---- UI component data ----------------------------------------------
    # Full props kept out of the model's context (lovia.web.put_ui_data);
    # they live as long as their chat.

    async def put_ui_data(self, session_id: str, payload: bytes) -> str | None:
        """Store a component's props (JSON bytes) for ``session_id``; return its ref.

        None when the chat no longer exists: a run cancelled by deleting its
        chat winds down in the background, and a tool still running must not
        leave data behind the delete.
        """
        ref = secrets.token_urlsafe(16)

        def _impl() -> bool:
            blob = zlib.compress(payload)
            with self._meta._tx() as conn:
                cur = conn.execute(
                    "INSERT INTO chat_ui_data (id, session_id, created_at, payload) "
                    "SELECT ?, ?, ?, ? WHERE EXISTS "
                    "(SELECT 1 FROM chat_sessions WHERE id = ?)",
                    (ref, session_id, time.time(), blob, session_id),
                )
                return cur.rowcount == 1

        return ref if await self._meta._run(_impl) else None

    async def get_ui_data(self, session_id: str, ref: str) -> bytes | None:
        """The JSON bytes stored under ``ref`` — None unless it belongs to ``session_id``."""
        return await self._read_one(
            "SELECT payload FROM chat_ui_data WHERE id = ? AND session_id = ?",
            (ref, session_id),
            lambda row: zlib.decompress(row[0]),
        )

    # ---- active run tracking --------------------------------------------

    async def get_active_run_id(self, session_id: str) -> str | None:
        """Return the run_id of the most recent unfinished run, or None."""
        return await self._read_one(
            "SELECT active_run_id FROM chat_sessions WHERE id = ?",
            (session_id,),
            lambda row: row[0],
        )

    async def set_active_run_id(
        self, session_id: str, run_id: str, *, context: JsonObject | None = None
    ) -> None:
        """Record that ``run_id`` is the active (potentially interrupted) run.

        ``context`` is the run's client context: a resume after a restart
        hands it to ``run_deps=`` again, since a checkpoint keeps none.
        """
        await self._write(
            "UPDATE chat_sessions SET active_run_id = ?, active_context_json = ? "
            "WHERE id = ?",
            (run_id, _dumps_context(context), session_id),
        )

    async def get_active_context(self, session_id: str) -> JsonObject | None:
        """The client context recorded with the active run, if any."""
        raw = await self._read_one(
            "SELECT active_context_json FROM chat_sessions WHERE id = ?",
            (session_id,),
            lambda row: row[0],
        )
        return json.loads(raw) if raw else None

    async def clear_active_run_id(
        self, session_id: str, *, expected: str | None = None
    ) -> None:
        """Clear the active run pointer (run completed or was abandoned).

        With ``expected`` set, only clears when the stored pointer still names
        that run — so a finished run doesn't wipe a pointer a newer run claimed.
        The run's client context goes with it.
        """
        if expected is None:
            await self._write(
                "UPDATE chat_sessions SET active_run_id = NULL, "
                "active_context_json = NULL WHERE id = ?",
                (session_id,),
            )
        else:
            await self._write(
                "UPDATE chat_sessions SET active_run_id = NULL, "
                "active_context_json = NULL WHERE id = ? AND active_run_id = ?",
                (session_id, expected),
            )

    # ---- schedules ------------------------------------------------------

    async def add_schedule(self, row: ScheduleRow) -> None:
        await self._write(
            f"INSERT INTO chat_schedules ({_SCHED_COLS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                row.until,
                row.max_fires,
                row.expires_at,
                row.fire_count,
                row.finished_reason,
                _dumps_context(row.context),
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
        """Overwrite every mutable column of the schedule (keyed by ``row.id``).

        ``fire_count`` is deliberately not written — it is scheduler-owned
        bookkeeping (see :meth:`mark_fired`), so an edit racing a fire can't
        rewind it.
        """
        await self._write(
            "UPDATE chat_schedules SET agent = ?, input = ?, session_id = ?, "
            "trigger_kind = ?, trigger_expr = ?, next_fire = ?, active = ?, "
            "last_session_id = ?, updated_at = ?, until = ?, max_fires = ?, "
            "expires_at = ?, finished_reason = ? WHERE id = ?",
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
                row.until,
                row.max_fires,
                row.expires_at,
                row.finished_reason,
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
        finished_reason: str | None = None,
    ) -> None:
        """Advance a schedule after a fire (or deactivate a one-shot).

        Every call counts one fire attempt (``fire_count``); pass
        ``finished_reason`` when this fire deactivates the schedule for good
        (e.g. its ``max_fires`` safety net tripped).

        This write races the run the fire just launched, which may have
        already ``cancel_schedule``d itself — so it is monotone: ``active``
        only ever decreases (``active * ?``) and a ``finished_reason`` that is
        already set is never erased (``COALESCE``). Advancing must not
        resurrect a schedule its own run just stopped.
        """
        await self._write(
            "UPDATE chat_schedules SET next_fire = ?, active = active * ?, "
            "last_session_id = ?, fire_count = fire_count + 1, "
            "finished_reason = COALESCE(finished_reason, ?), updated_at = ? "
            "WHERE id = ?",
            (
                next_fire,
                int(active),
                last_session_id,
                finished_reason,
                time.time(),
                schedule_id,
            ),
        )

    async def set_schedule_active(
        self,
        schedule_id: str,
        *,
        active: bool,
        next_fire: float | None = None,
        finished_reason: str | None = None,
    ) -> None:
        """Pause/resume a schedule (resume passes a freshly-computed next_fire).

        ``finished_reason`` is written verbatim: deactivation with a reason
        marks the schedule "done" (condition met / expired), while the default
        ``None`` keeps a plain pause — and clears the reason on resume.
        """
        if next_fire is None:
            await self._write(
                "UPDATE chat_schedules SET active = ?, finished_reason = ?, "
                "updated_at = ? WHERE id = ?",
                (int(active), finished_reason, time.time(), schedule_id),
            )
        else:
            await self._write(
                "UPDATE chat_schedules SET active = ?, next_fire = ?, "
                "finished_reason = ?, updated_at = ? WHERE id = ?",
                (int(active), next_fire, finished_reason, time.time(), schedule_id),
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

    async def list_children(self, parent_id: str) -> Sequence[ChatMeta]:
        """A chat's subagent task sessions, oldest first (t1, t2, …)."""
        return await self._read_all(
            f"SELECT {_META_COLS} FROM chat_sessions WHERE parent_id = ? "
            "ORDER BY created_at",
            (parent_id,),
            ChatMeta.from_row,
        )

    async def task_tree(self, session_id: str) -> Sequence[str]:
        """``session_id`` and its task sessions at any depth — what ``delete`` removes."""
        # UNION (not UNION ALL) ends a parent cycle.
        return await self._read_all(
            "WITH RECURSIVE tree(id) AS (SELECT ? UNION "
            "SELECT s.id FROM chat_sessions s JOIN tree ON s.parent_id = tree.id) "
            "SELECT id FROM tree",
            (session_id,),
            lambda row: row[0],
        )

    async def latest_run_statuses(self, session_ids: Sequence[str]) -> dict[str, str]:
        """Each session's most recent run status, in one query.

        Backs the sidebar's task badges: the sessions list joins this in for
        task sessions so the client needs no per-task follow-up requests.
        """
        out: dict[str, str] = {}
        ids = list(session_ids)
        # Chunk under SQLite's default 999-bind limit; pick each session's
        # newest row in SQL rather than scanning every run.
        for i in range(0, len(ids), 900):
            chunk = ids[i : i + 900]
            marks = ",".join("?" for _ in chunk)
            rows = await self._read_all(
                "SELECT r.session_id, r.status FROM chat_runs r JOIN ("
                "SELECT session_id, MAX(started_at) AS ts FROM chat_runs "
                f"WHERE session_id IN ({marks}) GROUP BY session_id"
                ") m ON r.session_id = m.session_id AND r.started_at = m.ts",
                tuple(chunk),
                lambda r: (r[0], r[1]),
            )
            out.update(rows)
        return out

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
