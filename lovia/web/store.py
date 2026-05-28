"""Chat store: a Session impl + a metadata table for the web UI.

The :class:`Session` Protocol only knows about ``load/append/clear`` for
transcript items — it has no concept of "list all my chats" or "what's the
title of this one". The web layer needs both, so we add a *parallel*
metadata table (``chat_sessions``) alongside whatever ``Session`` backend
is used for transcript storage.

Defaults to a SQLite file. Pass any other ``Session`` impl
(e.g. :class:`InMemorySession`) to keep transcripts elsewhere — only the
metadata table is owned by this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..session import Session
from ..stores import InMemorySession, SQLiteSession
from ..stores._sqlite import SQLiteStore

__all__ = ["ChatMeta", "ChatStore"]


_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    agent TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated
    ON chat_sessions(updated_at DESC);
"""


@dataclass(frozen=True)
class ChatMeta:
    """One row of the chat metadata table."""

    id: str
    title: str | None
    agent: str | None
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "agent": self.agent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ChatStore:
    """Session transcripts + chat metadata.

    Construction:

    * ``ChatStore.sqlite("./lovia.db")`` — persistent, single file (both
      transcripts and metadata in one DB).
    * ``ChatStore(InMemorySession(), meta_path=":memory:")`` — tests.
    * ``ChatStore(my_session, meta_path="…")`` — keep your custom session
      backend; we still get metadata.
    """

    def __init__(
        self,
        session: Session,
        *,
        meta_path: str | Path,
    ) -> None:
        self.session = session
        self._meta = SQLiteStore(str(meta_path), _META_SCHEMA)

    # ---- factories ------------------------------------------------------

    @classmethod
    def sqlite(cls, path: str | Path) -> "ChatStore":
        """Persistent store: both transcripts and metadata in one file."""
        return cls(SQLiteSession(path), meta_path=path)

    @classmethod
    def in_memory(cls) -> "ChatStore":
        """Volatile store for tests and one-off demos."""
        return cls(InMemorySession(), meta_path=":memory:")

    # ---- metadata -------------------------------------------------------

    async def upsert(
        self,
        session_id: str,
        *,
        agent: str | None = None,
    ) -> None:
        """Insert a row if missing, otherwise bump ``updated_at``."""
        now = time.time()

        def _impl() -> None:
            conn = self._meta._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO chat_sessions (id, title, agent, created_at, updated_at)
                    VALUES (?, NULL, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        agent = COALESCE(chat_sessions.agent, excluded.agent)
                    """,
                    (session_id, agent, now, now),
                )
                conn.commit()
            finally:
                self._meta._release(conn)

        await self._meta._run(_impl)

    async def set_title(self, session_id: str, title: str) -> None:
        title = title.strip()[:120]

        def _impl() -> None:
            conn = self._meta._connect()
            try:
                conn.execute(
                    "UPDATE chat_sessions SET title = ? WHERE id = ?",
                    (title, session_id),
                )
                conn.commit()
            finally:
                self._meta._release(conn)

        await self._meta._run(_impl)

    async def get(self, session_id: str) -> ChatMeta | None:
        def _impl() -> ChatMeta | None:
            conn = self._meta._connect()
            try:
                row = conn.execute(
                    "SELECT id, title, agent, created_at, updated_at "
                    "FROM chat_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    return None
                return ChatMeta(row[0], row[1], row[2], row[3], row[4])
            finally:
                self._meta._release(conn)

        return await self._meta._run(_impl)

    async def list(self, *, limit: int = 200) -> list[ChatMeta]:
        def _impl() -> list[ChatMeta]:
            conn = self._meta._connect()
            try:
                rows = conn.execute(
                    "SELECT id, title, agent, created_at, updated_at "
                    "FROM chat_sessions ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [ChatMeta(r[0], r[1], r[2], r[3], r[4]) for r in rows]
            finally:
                self._meta._release(conn)

        return await self._meta._run(_impl)

    async def delete(self, session_id: str) -> None:
        """Remove transcript AND metadata for ``session_id``."""
        await self.session.clear(session_id)

        def _impl() -> None:
            conn = self._meta._connect()
            try:
                conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
                conn.commit()
            finally:
                self._meta._release(conn)

        await self._meta._run(_impl)
