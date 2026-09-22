"""Shared SQLite plumbing for optional stdlib-backed stores."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from .._fdlimit import out_of_file_descriptors

logger = logging.getLogger(__name__)

T = TypeVar("T")

# How long a connection waits on a lock held by another connection (another
# process, or a sibling store writing to the same file) before raising
# "database is locked". Only applied when ``wal=True``.
_BUSY_TIMEOUT_MS = 5_000

# SQLite folds several transient conditions into OperationalError with nothing
# but the message to tell them apart, and only the ones that are provably
# *pre-commit* may be retried: CANTOPEN ("unable to open database file") comes
# from connect(), before a statement has run, and SQLITE_BUSY ("database is
# locked") leaves the transaction active and uncommitted.
#
# SQLITE_IOERR ("disk i/o error") is deliberately absent. It can surface from
# commit() after the writes already reached the file, and a retried
# SQLiteCheckpointer.append computes a fresh MAX(seq) + 1 and inserts the same
# entries under a second seq — a duplicated turn in whatever the run resumes
# from. Anything else — no such table, a malformed schema — is a bug, and
# retrying only delays the report.
_TRANSIENT = ("unable to open database file", "database is locked")

# Databases already reported as refusing WAL, by absolute path. The journal
# mode belongs to the file, not to a store or a connection, and one file
# routinely has several stores on it (``ChatStore.sqlite`` gives it three) —
# each of which would otherwise announce the same refusal.
_WAL_REFUSED: set[str] = set()
_RETRY_DELAYS = (0.05, 0.2, 0.5)


def _is_transient(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT)


def _explain_open_failure(exc: sqlite3.OperationalError) -> sqlite3.OperationalError:
    """Name the cause behind SQLite's catch-all open failure, when we can.

    SQLITE_CANTOPEN reads "unable to open database file" whether the directory
    is missing, the disk is full, or the process simply has no descriptor left
    — and the last is the one nobody guesses from the message. Returns ``exc``
    unchanged when that is not what happened. Which file it was stays with
    :meth:`SQLiteStore._run`, which logs it for every transient failure, not
    just this one.

    The original message stays as the prefix, so :func:`_is_transient` still
    recognizes the enriched error and :meth:`SQLiteStore._run` still retries it.
    """
    soft = out_of_file_descriptors()
    if soft is None:
        return exc
    return sqlite3.OperationalError(
        f"{exc} — the process is out of file descriptors "
        f"(soft limit {soft}); raise it with 'ulimit -n'"
    )


class SQLiteStore:
    """Small async bridge around stdlib sqlite3.

    Access is serialized with an asyncio lock, then executed in a worker
    thread so callers never block the event loop.

    Each call opens a fresh connection unless the path is ``:memory:``, in
    which case one connection is held open (each ``connect()`` to
    ``:memory:`` would otherwise return a brand-new, empty DB). The schema is
    ensured once per store instance, on the first connection — it lives in
    the database file, not the connection.

    ``wal=True`` opts a file-backed store into SQLite's WAL journal mode plus
    an explicit busy timeout: readers no longer block on a writer, and
    concurrent writers (another process, or several stores sharing one file)
    wait for the lock instead of failing fast. Off by default, which suits a
    lone store whose asyncio lock already serializes everything reaching the
    file; a *shared* file wants it on, because the stores sharing it hold
    separate locks and only the database sees the collision (see
    :meth:`~lovia.web.store.ChatStore.sqlite`, which turns it on for exactly
    that reason). Ignored for ``:memory:`` — a private in-memory DB has no
    second writer.

    WAL needs shared memory that a few filesystems (some network mounts) do
    not provide. SQLite keeps the old journal mode rather than failing there,
    so the store still works; the mode is read back and a refusal is logged
    once per database file — not once per store, since several of them share
    one file and the journal mode belongs to the file.
    """

    def __init__(self, path: str | Path, schema: str, *, wal: bool = False) -> None:
        self._path = str(path)
        self._schema = schema
        self._wal = wal and self._path != ":memory:"
        self._schema_ready = False
        self._lock = asyncio.Lock()
        self._shared: sqlite3.Connection | None = None
        if self._path == ":memory:":
            self._shared = sqlite3.connect(self._path, check_same_thread=False)
            self._shared.row_factory = sqlite3.Row
            self._shared.executescript(self._schema)
            self._shared.commit()
            self._schema_ready = True

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        try:
            conn = sqlite3.connect(self._path, check_same_thread=False)
        except sqlite3.OperationalError as exc:
            raise _explain_open_failure(exc) from exc
        conn.row_factory = sqlite3.Row
        if self._wal:
            # journal_mode is sticky on the file (re-setting is a cheap no-op);
            # busy_timeout is per-connection and must be set on every one.
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            if str(mode).lower() != "wal":
                # A refusal degrades to the old journal mode silently, and the
                # symptom is only slower reads under load. Say it once per
                # database: connections here are per-operation, and the stores
                # sharing one file would each repeat it.
                key = os.path.abspath(self._path)
                if key not in _WAL_REFUSED:
                    _WAL_REFUSED.add(key)
                    logger.warning(
                        "sqlite (%s): WAL was refused, journal mode is %r — "
                        "reads will block on writes; a filesystem without "
                        "shared memory (some network mounts) does this",
                        self._path,
                        mode,
                    )
        if not self._schema_ready:
            conn.executescript(self._schema)
            conn.commit()
            self._schema_ready = True
        return conn

    def _release(self, conn: sqlite3.Connection) -> None:
        """Close ``conn`` unless it's the shared in-memory handle."""
        if conn is not self._shared:
            conn.close()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """A connection for read-only work; released on exit."""
        conn = self._connect()
        try:
            yield conn
        finally:
            self._release(conn)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One transaction: commit on success, roll back on error.

        The rollback matters for the shared ``:memory:`` connection, which
        outlives the call — without it, statements left uncommitted by a
        mid-transaction failure would silently ride the NEXT operation's
        ``commit()``. (A file-backed connection gets an implicit rollback
        when the per-call connection closes; be explicit for both.)
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._release(conn)

    async def _run(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` off the event loop, riding out a transient store failure.

        ``fn`` must be exactly one transaction — every caller here goes through
        :meth:`_tx` or :meth:`_conn` — and every retried condition is one that
        cannot have committed (see :data:`_TRANSIENT`), so a failed attempt
        left nothing behind and re-running it is safe. Without this, a resource
        squeeze lasting milliseconds is fatal well beyond the statement that
        met it: the run loop treats a failed checkpoint as unrecoverable and
        aborts the run, and its terminal snapshot then fails on the same
        squeeze, so a transcript that was only *stale* is lost instead.

        The retries hold the store's lock, which is the point: a squeeze is a
        good moment for the rest of the process to wait rather than pile on.
        """
        async with self._lock:
            for delay in _RETRY_DELAYS:
                try:
                    return await asyncio.to_thread(fn)
                except sqlite3.OperationalError as exc:
                    if not _is_transient(exc):
                        raise
                    logger.warning(
                        "sqlite (%s): %s — retrying in %.2fs", self._path, exc, delay
                    )
                    await asyncio.sleep(delay)
            return await asyncio.to_thread(fn)
