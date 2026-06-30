"""Session protocol.

A :class:`Session` stores the conversation history for a multi-turn chat. It
is an intentionally minimal async protocol; concrete implementations live in
:mod:`lovia.stores`.

The runner accepts an optional ``Session``; if provided, it loads prior entries
as the canonical provider input and, when a run finishes, **appends** that run's
own new entries as one segment. The store is **append-only**: a completed run is
never rewritten, so prior history is immutable. Application code controls the
``session_id`` so multi-user systems just key sessions by user / conversation id.

What counts as *finished* is the caller's call. The runner auto-appends a
segment only when a run **completes successfully**; an interrupted run is left in
its checkpoint, not here. Recording an interrupted run instead — appending its
partial transcript as a finished segment and dropping the checkpoint — is a
deliberate **caller** decision, since only the caller knows whether the
interruption will be resumed or abandoned (the bundled web UI does this when a
user stops a run). A finalized partial must still be tool-consistent; see
:func:`lovia.transcript.drop_dangling_tool_calls`.

Why :class:`TranscriptEntry` and not :class:`Message`?
The TranscriptEntry form is richer (it preserves reasoning, server-side tool
calls, and provider-specific metadata). Chat-style adapters can still flatten
entries via :func:`lovia.transcript.entries_to_messages`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .types import JsonObject
from .transcript import TranscriptEntry


@dataclass
class Segment:
    """One completed run's contribution to a session.

    A session is an append-only log of these: each finished run appends its own
    ``entries`` as one segment keyed by ``run_id``, with opaque per-run ``meta``
    (e.g. a context policy's cross-run carryover, or a per-run summary). Stores
    persist ``meta`` verbatim and never interpret it.
    """

    run_id: str
    entries: list[TranscriptEntry]
    meta: JsonObject | None = None


# Reserved key under which the loop stows a JSON-safe snapshot of a run's last
# context-compaction in its finished segment's ``meta`` (a co-tenant alongside
# ``context_carryover``, defined in ``runtime.loop``). The web UI reads it to
# replay the compaction notice when a finished session is reloaded.
COMPACTED_META_KEY = "context_compacted"


class Session(Protocol):
    """A conversation transcript store keyed by ``session_id``.

    The contract is **append-only**: each finished run appends its own entries
    as one segment, ``segments`` returns them in run order, and stored history
    is never mutated. :meth:`segments` is the read primitive; :meth:`load` is a
    derived flat concatenation — a concrete default is provided, so an
    implementation that *subclasses* :class:`Session` only needs ``segments``,
    ``append``, and ``clear``. There is deliberately no ``replace`` — immutable
    history is what lets the runner persist only a run's delta and keeps
    cross-run state (run boundaries, per-run ``meta``) consistent.
    """

    async def segments(self, session_id: str) -> list[Segment]:
        """Return every run :class:`Segment` for ``session_id``, in run order."""
        ...

    async def load(self, session_id: str) -> list[TranscriptEntry]:
        """Return the full transcript for ``session_id`` as one flat list.

        Defaults to flattening :meth:`segments`; a store may override it with a
        cheaper read.
        """
        out: list[TranscriptEntry] = []
        for seg in await self.segments(session_id):
            out.extend(seg.entries)
        return out

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
