"""In-memory implementations of :class:`Session` and :class:`MemoryStore`.

These backends keep everything in process; perfect for tests, CLIs, or
single-process applications. For anything else, persist to SQLite or a
custom backend.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from ..messages import ChatMessage


class InMemorySession:
    """A :class:`Session` that keeps transcripts in a dict."""

    def __init__(self) -> None:
        self._data: dict[str, list[ChatMessage]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def load(self, session_id: str) -> list[ChatMessage]:
        async with self._lock:
            return list(self._data.get(session_id, []))

    async def append(self, session_id: str, messages: list[ChatMessage]) -> None:
        async with self._lock:
            self._data[session_id].extend(messages)

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            self._data.pop(session_id, None)


class InMemoryMemoryStore:
    """A :class:`MemoryStore` backed by a dict."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            return self._data.get(key)

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            self._data[key] = value

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def list(self, prefix: str = "") -> list[tuple[str, str]]:
        async with self._lock:
            return [(k, v) for k, v in self._data.items() if k.startswith(prefix)]
