"""In-memory implementation of :class:`Session`.

Keeps transcripts in process; perfect for tests, CLIs, or single-process
applications. For anything else, persist to SQLite or a custom backend.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from ..transcript import TranscriptEntry


class InMemorySession:
    """A :class:`Session` that keeps transcripts in a dict."""

    def __init__(self) -> None:
        self._data: dict[str, list[TranscriptEntry]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def load(self, session_id: str) -> list[TranscriptEntry]:
        async with self._lock:
            return list(self._data.get(session_id, []))

    async def append(self, session_id: str, entries: list[TranscriptEntry]) -> None:
        async with self._lock:
            self._data[session_id].extend(entries)

    async def replace(self, session_id: str, entries: list[TranscriptEntry]) -> None:
        async with self._lock:
            self._data[session_id] = list(entries)

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            self._data.pop(session_id, None)
