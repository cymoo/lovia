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
    MessageOut,
    RewindRequest,
    RewindResponse,
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
    segments_to_out,
    session_info,
    view_messages,
)

_RESUMABLE = ("interrupted", "running")


async def _session_view(
    deps: RouterDeps, session_id: str, *, created_at: float, updated_at: float
) -> tuple[list[MessageOut], str | None]:
    """Resolve the transcript view + reconnect pointer for one session.

    * Live supervised run → flat history spliced with the checkpoint's entries
      so far, advertising the run's id (even before its first checkpoint) so
      the client reconnects — attach then delivers the authoritative snapshot.
    * No live run but a resumable checkpoint (restart recovery) → the same
      splice, advertising the stored pointer; a stale pointer is cleaned up.
    * Finished → rebuilt from segments so persisted per-run compaction notices
      replay (a live run surfaces compaction over SSE instead).
    """
    store, session = deps.store, deps.session

    live = deps.supervisor.get(session_id)
    if live is not None:
        entries = await session.load(session_id)
        if store.checkpointer is not None and live.run_id is not None:
            snapshot = await store.checkpointer.load(live.run_id)
            if snapshot is not None and snapshot.status in _RESUMABLE:
                entries = entries + list(snapshot.entries)
        view = view_messages(entries, created_at=created_at, updated_at=updated_at)
        return view, live.run_id

    candidate = (
        await store.get_active_run_id(session_id)
        if store.checkpointer is not None
        else None
    )
    if candidate and store.checkpointer is not None:
        snapshot = await store.checkpointer.load(candidate)
        if snapshot is not None and snapshot.status in _RESUMABLE:
            entries = await session.load(session_id) + list(snapshot.entries)
            view = view_messages(entries, created_at=created_at, updated_at=updated_at)
            return view, candidate
        # Stale pointer — clean it up so a reload doesn't offer a dead reconnect.
        await store.clear_active_run_id(session_id)
        if snapshot is not None:
            await store.checkpointer.delete(candidate)

    segments = await session.segments(session_id)
    return (
        segments_to_out(segments, created_at=created_at, updated_at=updated_at),
        None,
    )


def build_sessions_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()
    store = deps.store
    session = deps.session

    @router.get("/api/sessions", response_model=list[ChatSessionInfo])
    async def list_sessions(
        q: str = Query("", max_length=200),
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> list[ChatSessionInfo]:
        metas = (
            await store.search(q, limit=limit, offset=offset)
            if q
            else await store.list(limit=limit, offset=offset)
        )
        return [session_info(m) for m in metas]

    @router.get("/api/runs", response_model=list[RunInfo])
    async def list_runs() -> list[RunInfo]:
        """Currently-live supervised runs (in-memory; authoritative for active)."""
        return [
            RunInfo(
                session_id=sid,
                run_id=c.run_id,
                agent=deps.name_of(c.agent),  # registry key, same as AgentInfo
                status=c.status,
                turns=c.turns,
            )
            for sid, c in deps.supervisor
        ]

    @router.delete("/api/sessions")
    async def delete_all_sessions() -> dict[str, bool]:
        """Delete every session's transcript and metadata (stopping live runs)."""
        for sid, _ctrl in deps.supervisor:
            deps.supervisor.cancel(sid, discard=True)
        await store.delete_all()
        return {"ok": True}

    @router.get("/api/sessions/{session_id}", response_model=SessionDetail)
    async def get_session(session_id: str) -> SessionDetail:
        meta = await store.get(session_id)
        now = time.time()
        created = meta.created_at if meta else now
        updated = meta.updated_at if meta else now
        entries, active_run_id = await _session_view(
            deps, session_id, created_at=created, updated_at=updated
        )
        return SessionDetail(
            id=meta.id if meta else session_id,
            title=meta.title if meta else None,
            agent=meta.agent if meta else None,
            created_at=created,
            updated_at=updated,
            entries=entries,
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
        # Stop a live run first (discarding its partial transcript) — otherwise
        # it keeps burning tokens and re-persists entries for the deleted chat
        # when it winds down.
        deps.supervisor.cancel(session_id, discard=True)
        await store.delete(session_id)
        return {"ok": True}

    @router.post("/api/sessions/{session_id}/rewind", response_model=RewindResponse)
    async def rewind_session(session_id: str, req: RewindRequest) -> RewindResponse:
        """Rewind to just before the ``user_turn``-th user message.

        The destructive-undo behind edit-and-resend / regenerate: the target
        user message and everything after it are dropped; the caller then
        sends the (edited or original) text as a fresh turn. Refused while a
        run is live — its in-flight state would resurrect the tail.
        """
        if deps.supervisor.get(session_id) is not None:
            raise HTTPException(
                status_code=409,
                detail="a run is active for this session; stop it first",
            )
        rewind = getattr(session, "rewind", None)
        if rewind is None:
            raise HTTPException(
                status_code=501,
                detail="the configured session store does not support rewind",
            )
        # Any stored resume pointer dies with every rewind — and its snapshot
        # may hold user turns the client rendered (the spliced view) that the
        # store doesn't. Drop it FIRST, remembering how many user turns it
        # contributed, so ordinal mapping agrees with what the client saw.
        ckpt_user_turns = 0
        stale = await store.get_active_run_id(session_id)
        if stale:
            if store.checkpointer is not None:
                snapshot = await store.checkpointer.load(stale)
                if snapshot is not None and snapshot.status in _RESUMABLE:
                    ckpt_user_turns = sum(
                        1
                        for e in snapshot.entries
                        if isinstance(e, InputEntry) and e.role == "user"
                    )
                await store.checkpointer.delete(stale)
            await store.clear_active_run_id(session_id, expected=stale)
        # Map the user-turn ordinal onto the flat entry index that starts it.
        entries = await session.load(session_id)
        cut = None
        seen = -1
        for i, entry in enumerate(entries):
            if isinstance(entry, InputEntry) and entry.role == "user":
                seen += 1
                if seen == req.user_turn:
                    cut = i
                    break
        if cut is None:
            if req.user_turn <= seen + ckpt_user_turns:
                # The target lived only in the just-dropped checkpoint:
                # rewinding to before it keeps the whole stored transcript.
                removed = 0
            else:
                raise HTTPException(
                    status_code=404, detail=f"user turn {req.user_turn} not found"
                )
        else:
            removed = await rewind(session_id, keep_entries=cut)
        meta = await store.get(session_id)
        now = time.time()
        view, _ = await _session_view(
            deps,
            session_id,
            created_at=meta.created_at if meta else now,
            updated_at=meta.updated_at if meta else now,
        )
        return RewindResponse(removed=removed, entries=view)

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
