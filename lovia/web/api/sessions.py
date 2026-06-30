"""Session routes: list, fetch, rename, delete, todos, and export."""

from __future__ import annotations

import time
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import JSONResponse, PlainTextResponse
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...plugins import todos_from_entries
from ...transcript import InputEntry, TranscriptEntry, entries_to_messages
from ..schemas import (
    ChatSessionInfo,
    RunInfo,
    SessionDetail,
    SessionPatch,
    TodoItemOut,
    TodosResponse,
)
from .deps import RouterDeps
from .serialization import (
    export_md,
    export_txt,
    message_to_json_dict,
    messages_to_out,
    segments_to_out,
    session_info,
)


def build_sessions_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()
    store = deps.store
    session = deps.session

    @router.get("/api/sessions", response_model=list[ChatSessionInfo])
    async def list_sessions(
        q: str = Query("", max_length=200),
        limit: int = Query(200, ge=1, le=1000),
    ) -> list[ChatSessionInfo]:
        metas = (
            await store.search(q, limit=limit)
            if q
            else await store.list_all(limit=limit)
        )
        return [session_info(m) for m in metas]

    @router.get("/api/runs", response_model=list[RunInfo])
    async def list_runs(
        status: str = Query("active", pattern="^active$"),
    ) -> list[RunInfo]:
        """Currently-live supervised runs (in-memory; authoritative for active)."""
        return [
            RunInfo(
                session_id=sid,
                run_id=c.run_id,
                agent=c.agent.name,
                status=c.status,
                turns=c.turns,
            )
            for sid, c in deps.supervisor
        ]

    @router.delete("/api/sessions")
    async def delete_all_sessions() -> dict[str, bool]:
        """Delete every session's transcript and metadata."""
        await store.delete_all()
        return {"ok": True}

    @router.get("/api/sessions/{session_id}", response_model=SessionDetail)
    async def get_session(session_id: str) -> SessionDetail:
        meta = await store.get(session_id)
        # A live supervised run owns the session: report its run_id (even before
        # the first checkpoint) and don't treat the pointer as stale. The client
        # reconnects → attach delivers the authoritative snapshot anyway.
        live = deps.supervisor.get(session_id)
        active_run_id: str | None = live.run_id if live is not None else None

        # A finished session (no live run, no resumable checkpoint) is rebuilt
        # from segments so persisted per-run compaction notices replay. A live or
        # resuming run splices checkpoint entries on top of the flat transcript and
        # surfaces compaction over the live SSE stream instead.
        entries: list[TranscriptEntry] = []
        finished = False

        if live is not None:
            entries = await session.load(session_id)
            if store.checkpointer is not None and live.run_id is not None:
                snapshot = await store.checkpointer.load(live.run_id)
                if snapshot is not None and snapshot.status in (
                    "interrupted",
                    "running",
                ):
                    entries = entries + [
                        e
                        for e in snapshot.entries
                        if not (isinstance(e, InputEntry) and e.role == "system")
                    ]
        elif store.checkpointer is not None:
            # No live run, but a checkpoint may hold an interrupted run to resume
            # (restart recovery). Prefer its entries; clean up a stale pointer.
            candidate = await store.get_active_run_id(session_id)
            snapshot = await store.checkpointer.load(candidate) if candidate else None
            if (
                candidate
                and snapshot is not None
                and snapshot.status in ("interrupted", "running")
            ):
                history = await session.load(session_id)
                run_entries = [
                    e
                    for e in snapshot.entries
                    if not (isinstance(e, InputEntry) and e.role == "system")
                ]
                entries = history + run_entries
                active_run_id = candidate
            else:
                if candidate:
                    await store.clear_active_run_id(session_id)
                    if snapshot is not None:
                        await store.checkpointer.delete(candidate)
                finished = True
        else:
            finished = True

        now = time.time()
        created = meta.created_at if meta else now
        updated = meta.updated_at if meta else now
        if finished:
            out_entries = segments_to_out(
                await session.segments(session_id),
                created_at=created,
                updated_at=updated,
            )
        else:
            out_entries = messages_to_out(
                entries_to_messages(entries), created_at=created, updated_at=updated
            )
        return SessionDetail(
            id=meta.id if meta else session_id,
            title=meta.title if meta else None,
            agent=meta.agent if meta else None,
            created_at=created,
            updated_at=updated,
            entries=out_entries,
            active_run_id=active_run_id,
        )

    @router.patch("/api/sessions/{session_id}", response_model=ChatSessionInfo)
    async def update_session(session_id: str, req: SessionPatch) -> ChatSessionInfo:
        """Rename and/or (un)pin a session — applies whichever fields are set."""
        meta = await store.get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        if req.title is not None:
            await store.set_title(session_id, req.title)
        if req.pinned is not None:
            await store.set_pinned(session_id, req.pinned)
        meta = await store.get(session_id)
        if meta is None:  # deleted concurrently between the update and re-read
            raise HTTPException(status_code=404, detail="session not found")
        return session_info(meta)

    @router.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        await store.delete(session_id)
        return {"ok": True}

    @router.get("/api/sessions/{session_id}/todos", response_model=TodosResponse)
    async def get_todos(session_id: str) -> TodosResponse:
        """Latest todo list for a session, reconstructed from its transcript."""
        entries = await session.load(session_id)
        todos = todos_from_entries(entries)
        return TodosResponse(
            todos=[
                TodoItemOut(
                    content=t.content, status=t.status, active_form=t.active_form
                )
                for t in todos
            ]
        )

    @router.get("/api/sessions/{session_id}/export")
    async def export_session(
        session_id: str, format: str = Query("md", pattern="^(md|json|txt)$")
    ) -> Any:
        """Export a session as markdown, JSON, or plain text."""
        meta = await store.get(session_id)
        msgs = entries_to_messages(await session.load(session_id))

        if format == "json":
            return JSONResponse(
                content={
                    "session_id": session_id,
                    "title": meta.title if meta else None,
                    "agent": meta.agent if meta else None,
                    "messages": [message_to_json_dict(m) for m in msgs],
                }
            )

        filename = f"lovia-{session_id[:8]}.{format}"
        if format == "txt":
            return PlainTextResponse(
                export_txt(msgs),
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )

        return PlainTextResponse(
            export_md(
                msgs,
                title=(meta.title if meta else None) or "Chat",
                session_id=session_id,
            ),
            media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return router
