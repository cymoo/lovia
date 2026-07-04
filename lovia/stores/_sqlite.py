"""Shared SQLite plumbing for optional stdlib-backed stores."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

T = TypeVar("T")

# How long a connection waits on a lock held by another connection (another
# process, or a sibling store writing to the same file) before raising
# "database is locked". Only applied when ``wal=True``.
_BUSY_TIMEOUT_MS = 5_000


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
    wait for the lock instead of failing fast. Off by default — a
    single-process store serialized by the asyncio lock does not need it.
    Ignored for ``:memory:`` (a private in-memory DB has no second writer).
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
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if self._wal:
            # journal_mode is sticky on the file (re-setting is a cheap no-op);
            # busy_timeout is per-connection and must be set on every one.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
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
        async with self._lock:
            return await asyncio.to_thread(fn)
