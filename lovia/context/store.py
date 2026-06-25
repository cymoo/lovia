"""Pluggable storage for offloaded tool results.

When a context policy offloads a large tool result it writes the full output
to a :class:`ResultStore` and keeps only a short marker in the per-call view;
the policy's recall tool reads it back by ``call_id``. The store is owned by
the *policy*, not the runner, so the context layer never depends on the
workspace (or any runner-provided capability) just to archive a result.

The full output also stays in the transcript, so the store is a **cache**: a
dropped entry — or an ephemeral :class:`InMemoryResultStore` lost on restart —
degrades to the transcript, which the recall tool falls back to. There is
deliberately **no** ``delete``/eviction on the protocol: nothing relies on it
for correctness, and a backend that wants a bound can enforce one internally
(see :class:`InMemoryResultStore`). Selective/partial reads are likewise left
out until a concrete need lands — both are non-breaking to add later.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from pathlib import Path
from typing import Protocol
from urllib.parse import quote


class ResultStore(Protocol):
    """A minimal ``key -> text`` blob store for offloaded tool results."""

    async def put(self, key: str, content: str) -> None:
        """Store ``content`` under ``key`` (overwriting any prior value)."""
        ...

    async def get(self, key: str) -> str | None:
        """Return the content stored under ``key``, or ``None`` if absent."""
        ...


class InMemoryResultStore:
    """A :class:`ResultStore` backed by an in-process dict, LRU-bounded.

    Ephemeral: contents vanish when the process exits, which is safe because
    the full output also lives in the transcript (recall falls back to it).
    **Bounded by default** (``max_entries``): once full, the least-recently-used
    key is evicted — also safe, since eviction just sends recall back to the
    transcript. A policy (and therefore its store) is typically constructed once
    and shared across sessions, so the bound is what stops a long-lived server
    from accumulating every session's offloaded outputs forever. Pass
    ``max_entries=None`` to opt into unbounded retention.
    """

    def __init__(self, *, max_entries: int | None = 1024) -> None:
        if max_entries is not None and max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max = max_entries
        self._data: "OrderedDict[str, str]" = OrderedDict()

    async def put(self, key: str, content: str) -> None:
        self._data[key] = content
        self._data.move_to_end(key)
        if self._max is not None:
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    async def get(self, key: str) -> str | None:
        value = self._data.get(key)
        if value is not None:
            self._data.move_to_end(key)
        return value


class FileResultStore:
    """A :class:`ResultStore` that writes each value to a file under ``dir``.

    Durable across restarts (unlike :class:`InMemoryResultStore`). It does
    **not** evict — recall falls back to the transcript if a file is gone, but
    operators should apply their own retention/cleanup to ``dir`` since it grows
    with every offloaded result. Keys are mapped to injective, length-bounded
    file names; file I/O runs on a thread so it never blocks the event loop.
    """

    def __init__(self, dir: str | Path) -> None:
        self._dir = Path(dir)

    def _path(self, key: str) -> Path:
        return self._dir / f"{_safe_key(key)}.txt"

    async def put(self, key: str, content: str) -> None:
        def _write() -> None:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> str | None:
        def _read() -> str | None:
            try:
                return self._path(key).read_text(encoding="utf-8")
            except OSError:
                # Missing/unreadable file is a cache miss — recall then falls
                # back to the transcript rather than erroring.
                return None

        return await asyncio.to_thread(_read)


def _safe_key(key: str) -> str:
    # Injective, filesystem-safe, and length-bounded. A lossy or unbounded
    # sanitizer could (a) collide distinct keys onto one file — and since recall
    # prefers the store, return the *wrong* output — or (b) overflow the ~255-char
    # filename limit on long/multibyte ids. The sha1 suffix guarantees a unique,
    # non-empty name; the readable prefix is just for debugging.
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{quote(key, safe='')[:80]}-{digest}"


__all__ = ["FileResultStore", "InMemoryResultStore", "ResultStore"]
