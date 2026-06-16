"""Session protocol.

A :class:`Session` stores the conversation history for a multi-turn chat. It
is an intentionally minimal async protocol; concrete implementations live in
:mod:`lovia.stores`.

The runner accepts an optional ``Session``; if provided, it loads prior entries,
keeps them as the canonical provider input, and persists the updated transcript
when the run finishes. Application code controls the ``session_id`` so
multi-user systems just key sessions by user / conversation id.

Why :class:`TranscriptEntry` and not :class:`Message`?
The TranscriptEntry form is richer (it preserves reasoning, server-side tool
calls, and provider-specific metadata). Chat-style adapters can still flatten
entries via :func:`lovia.transcript.entries_to_messages`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .transcript import TranscriptEntry


@runtime_checkable
class Session(Protocol):
    """A conversation transcript store keyed by ``session_id``."""

    async def load(self, session_id: str) -> list[TranscriptEntry]: ...

    async def replace(self, session_id: str, entries: list[TranscriptEntry]) -> None:
        """Atomically replace the stored transcript for ``session_id``.

        Used by the runner to persist the run's full transcript and by callers
        that explicitly edit history. Context compaction never calls this — it
        only shapes the per-call view and leaves the Session untouched.
        Implementations should make this transactional; partial replacement
        leaves a corrupt transcript.
        """
        ...

    async def clear(self, session_id: str) -> None: ...
