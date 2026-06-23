"""Session protocol.

A :class:`Session` stores the conversation history for a multi-turn chat. It
is an intentionally minimal async protocol; concrete implementations live in
:mod:`lovia.stores`.

The runner accepts an optional ``Session``; if provided, it loads prior entries
as the canonical provider input and, when a run finishes, **appends** that run's
own new entries as one segment. The store is **append-only**: a completed run is
never rewritten, so prior history is immutable. Application code controls the
``session_id`` so multi-user systems just key sessions by user / conversation id.

Why :class:`TranscriptEntry` and not :class:`Message`?
The TranscriptEntry form is richer (it preserves reasoning, server-side tool
calls, and provider-specific metadata). Chat-style adapters can still flatten
entries via :func:`lovia.transcript.entries_to_messages`.
"""

from __future__ import annotations

from typing import Protocol

from .types import JsonObject
from .transcript import TranscriptEntry


class Session(Protocol):
    """A conversation transcript store keyed by ``session_id``.

    The contract is **append-only**: each finished run appends its own entries
    as one segment, ``load`` returns the flat concatenation of all segments,
    and stored history is never mutated. There is deliberately no ``replace`` —
    immutable history is what lets the runner persist only a run's delta and
    keeps cross-run state (run boundaries, future per-run metadata) consistent.
    """

    async def load(self, session_id: str) -> list[TranscriptEntry]:
        """Return the full transcript for ``session_id`` as one flat list."""
        ...

    async def append(
        self,
        session_id: str,
        entries: list[TranscriptEntry],
        *,
        run_id: str | None = None,
        meta: JsonObject | None = None,
    ) -> str:
        """Append one run's ``entries`` as a new segment; return its ``run_id``.

        ``run_id`` identifies the run and, when checkpointing, ties the segment
        to its checkpoint. Append is **idempotent** on it: appending again for a
        ``run_id`` already stored under ``session_id`` is a no-op, so re-issuing a
        completed run never duplicates it. When ``run_id`` is omitted the store
        generates a unique one and returns it. ``meta`` is opaque per-segment
        metadata (e.g. a future per-run summary/output); stores persist it
        verbatim and need not interpret it. The append should be atomic.
        """
        ...

    async def clear(self, session_id: str) -> None:
        """Drop the entire transcript for ``session_id``."""
        ...
