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
    """

    def __init__(self, path: str | Path, schema: str) -> None:
        self._path = str(path)
        self._schema = schema
        self._lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not self._initialized:
            conn.executescript(self._schema)
            conn.commit()
            self._initialized = True
        return conn

    async def _run(self, fn: Callable[[], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn)
