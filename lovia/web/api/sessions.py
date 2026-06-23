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
from ...transcript import InputEntry, entries_to_messages
from ..schemas import (
    ChatSessionInfo,
    RenameRequest,
    SessionDetail,
    TodoItemOut,
    TodosResponse,
)
from .deps import RouterDeps
from .serialization import (
    export_md,
    export_txt,
    message_to_json_dict,
    messages_to_out,
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

    @router.delete("/api/sessions")
    async def delete_all_sessions() -> dict[str, bool]:
        """Delete every session's transcript and metadata."""
        await store.delete_all()
        return {"ok": True}

    @router.get("/api/sessions/{session_id}", response_model=SessionDetail)
    async def get_session(session_id: str) -> SessionDetail:
        meta = await store.get(session_id)
        active_run_id: str | None = None

        # Prefer an interrupted run's checkpoint entries when they're more
        # up-to-date than the session store (only persisted on success).
        if store.checkpointer is not None:
            candidate = await store.get_active_run_id(session_id)
            if candidate:
                snapshot = await store.checkpointer.load(candidate)
                if snapshot is not None and snapshot.status in (
                    "interrupted",
                    "running",
                ):
                    # The checkpoint holds only the in-flight run's own entries;
                    # prepend the persisted history for the full conversation.
                    # (Strip any system entry defensively — it's re-generated.)
                    history = await session.load(session_id)
                    run_entries = [
                        e
                        for e in snapshot.entries
                        if not (isinstance(e, InputEntry) and e.role == "system")
                    ]
                    entries = history + run_entries
                    active_run_id = candidate
                else:
                    # Stale pointer (failed, completed, or deleted): clean up.
                    await store.clear_active_run_id(session_id)
                    if snapshot is not None:
                        await store.checkpointer.delete(candidate)
                    entries = await session.load(session_id)
            else:
                entries = await session.load(session_id)
        else:
            entries = await session.load(session_id)

        msgs = entries_to_messages(entries)
        now = time.time()
        created = meta.created_at if meta else now
        updated = meta.updated_at if meta else now
        return SessionDetail(
            id=meta.id if meta else session_id,
            title=meta.title if meta else None,
            agent=meta.agent if meta else None,
            created_at=created,
            updated_at=updated,
            entries=messages_to_out(msgs, created_at=created, updated_at=updated),
            active_run_id=active_run_id,
        )

    @router.patch("/api/sessions/{session_id}", response_model=ChatSessionInfo)
    async def rename_session(session_id: str, req: RenameRequest) -> ChatSessionInfo:
        meta = await store.get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        await store.set_title(session_id, req.title)
        meta = await store.get(session_id)
        assert meta is not None  # just updated
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
                TodoItemOut(content=t.content, status=t.status, active_form=t.active_form)
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
