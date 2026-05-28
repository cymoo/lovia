"""Shared SQLite plumbing for optional stdlib-backed stores."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


class SQLiteStore:
    """Small async bridge around stdlib sqlite3.

    Access is serialized with an asyncio lock, then executed in a worker
    thread so callers never block the event loop.

    Each call opens a fresh connection unless the path is ``:memory:``, in
    which case we hold one connection open (otherwise each ``connect()``
    would return a brand-new, empty in-memory DB). For on-disk DBs we
    re-create-on-open so schemas stay current (CREATE TABLE IF NOT EXISTS
    is idempotent).
    """

    def __init__(self, path: str | Path, schema: str) -> None:
        self._path = str(path)
        self._schema = schema
        self._lock = asyncio.Lock()
        self._shared: sqlite3.Connection | None = None
        if self._path == ":memory:":
            self._shared = sqlite3.connect(self._path, check_same_thread=False)
            self._shared.row_factory = sqlite3.Row
            self._shared.executescript(self._schema)
            self._shared.commit()

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(self._schema)
        conn.commit()
        return conn

    def _release(self, conn: sqlite3.Connection) -> None:
        """Close ``conn`` unless it's the shared in-memory handle."""
        if conn is not self._shared:
            conn.close()

    async def _run(self, fn: Callable[[], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn)
