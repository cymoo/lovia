"""In-memory implementation of :class:`Session`.

Keeps transcripts in process; perfect for tests, CLIs, or single-process
applications. For anything else, persist to SQLite or a custom backend.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from ..items import Item


class InMemorySession:
    """A :class:`Session` that keeps transcripts in a dict."""

    def __init__(self) -> None:
        self._data: dict[str, list[Item]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def load(self, session_id: str) -> list[Item]:
        async with self._lock:
            return list(self._data.get(session_id, []))

    async def append(self, session_id: str, items: list[Item]) -> None:
        async with self._lock:
            self._data[session_id].extend(items)

    async def replace(self, session_id: str, items: list[Item]) -> None:
        async with self._lock:
            self._data[session_id] = list(items)

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            self._data.pop(session_id, None)
