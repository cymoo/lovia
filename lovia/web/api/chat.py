"""Chat routes: blocking turn, SSE stream/attach, approval, cancel, reconnect.

Streaming runs are owned by the :class:`~lovia.web.supervisor.RunSupervisor`,
not the request: ``/chat/stream`` starts (or attaches to) a supervised run and
forwards its event hub to SSE. A disconnect detaches; the run keeps going.
"""

from __future__ import annotations

import uuid
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Query
    from sse_starlette.sse import EventSourceResponse
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...runner import Runner
from ..schemas import (
    ApprovalRequest,
    ChatRequest,
    ChatResponse,
    InjectCancelRequest,
    InjectRequest,
)
from ..sse import _coerce, usage_dict
from ..supervisor import RunController, forward
from ..titles import provisional_title
from .deps import RouterDeps


def build_chat_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()
    store = deps.store
    session = deps.session

    async def upsert_session(sid: str, agent_name: str, message: str) -> bool:
        """Insert/touch the session's metadata row; returns whether it's new."""
        is_new = (await store.get(sid)) is None
        await store.upsert(
            sid,
            agent=agent_name,
            title=provisional_title(message) if is_new else None,
        )
        return is_new

    @router.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        # Blocking, non-streaming turn — runs to completion inside the request
        # and is NOT supervised (not detachable).
        if not req.message.strip():
            raise HTTPException(status_code=422, detail="empty message")
        agent = deps.pick(req.agent)
        sid = req.session_id or uuid.uuid4().hex
        if deps.supervisor.get(sid) is not None:
            # A supervised run owns this session; a second concurrent run would
            # interleave two transcripts. Stream endpoints attach/inject instead.
            raise HTTPException(
                status_code=409,
                detail="a streaming run is active for this session; "
                "use /api/chat/stream to attach or inject",
            )
        is_new = await upsert_session(sid, agent.name, req.message)
        result = await Runner.run(
            agent,
            req.message,
            session=session,
            session_id=sid,
            context_policy=deps.context_policy,
            max_turns=deps.max_turns,
            budget=deps.fresh_budget(),
            retry=deps.retry,
            tracer=deps.tracer,
        )
        if is_new:
            deps.schedule_title(sid, req.message, result.output, agent.name)
        return ChatResponse(
            output=_coerce(result.output),
            session_id=sid,
            usage=usage_dict(result.usage),
        )

    @router.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest) -> EventSourceResponse:
        agent = deps.pick(req.agent)
        sid = req.session_id or uuid.uuid4().hex
        # An empty message is only meaningful as a pure attach to a live run;
        # rejecting it before the upsert avoids littering empty "New chat" rows.
        if not req.message.strip() and deps.supervisor.get(sid) is None:
            raise HTTPException(status_code=422, detail="empty message")
        is_new = await upsert_session(sid, agent.name, req.message)

        def attach(live: RunController) -> EventSourceResponse:
            # A run is already live for this session: a new message injects
            # (Phase 1); this connection attaches to co-watch it.
            if req.message.strip():
                live.inject(req.message)
            return EventSourceResponse(
                forward(live.attach(with_snapshot=True), sid=sid, emit_session=True)
            )

        live = deps.supervisor.get(sid)
        if live is not None:
            return attach(live)
        if not req.message.strip():
            # The live run we would have attached to ended mid-request.
            raise HTTPException(status_code=422, detail="empty message")

        # No live run → start a fresh supervised run. Delete any stranded
        # checkpoint first so a later reconnect won't pick up a stale snapshot.
        if store.checkpointer is not None:
            old_run_id = await store.get_active_run_id(sid)
            if old_run_id:
                await store.checkpointer.delete(old_run_id)

        try:
            ctrl = await deps.supervisor.start(
                session_id=sid,
                agent=agent,
                input=req.message,
                is_new=is_new,
                title_message=req.message,
            )
        except HTTPException as exc:
            # Lost a concurrent-start race (two tabs submitting at once): the
            # winner owns the run, so deliver this message by injecting into it.
            live = deps.supervisor.get(sid)
            if exc.status_code == 409 and live is not None:
                return attach(live)
            raise
        return EventSourceResponse(
            forward(ctrl.subscribe_live(), sid=sid, emit_session=True)
        )

    @router.post("/api/chat/inject")
    async def chat_inject(req: InjectRequest) -> dict[str, Any]:
        """Queue a message into the active run for ``session_id``.

        ``{"accepted": true, "id": <token>}`` when a run is live (drained at the
        next turn start; the token withdraws it via ``/uninject``).
        ``{"accepted": false}`` when no run is active — the "run just ended" race —
        so the client can fall back to a normal stream without losing the message.
        """
        message = req.message.strip()
        if not message:
            raise HTTPException(status_code=422, detail="empty message")
        ctrl = deps.supervisor.get(req.session_id)
        if ctrl is None:
            return {"accepted": False}
        return {"accepted": True, "id": ctrl.inject(message)}

    @router.post("/api/chat/uninject")
    async def chat_uninject(req: InjectCancelRequest) -> dict[str, bool]:
        """Withdraw a still-queued message before the run drains it.

        ``{"removed": false}`` if it was already consumed or no run is active.
        """
        ctrl = deps.supervisor.get(req.session_id)
        return {"removed": bool(ctrl is not None and ctrl.uninject(req.id))}

    @router.post("/api/chat/approve")
    async def approve(req: ApprovalRequest) -> dict[str, bool]:
        ok = await deps.approvals.resolve(
            req.session_id, req.call_id, req.decision == "approve"
        )
        if not ok:
            raise HTTPException(status_code=404, detail="no pending approval matches")
        return {"ok": True}

    @router.post("/api/chat/cancel")
    async def cancel_stream(session_id: str = Query(...)) -> dict[str, bool]:
        """Cancel the live run for ``session_id`` (or clear a stranded checkpoint)."""
        ctrl = deps.supervisor.get(session_id)
        if ctrl is not None:
            run_id = ctrl.run_id
            deps.supervisor.cancel(session_id)
            # cancel() evicts the controller synchronously, so until its task's
            # finally runs the active_run_id pointer still names this run. Clear
            # it eagerly (guarded so we can't clobber a newer run) — otherwise a
            # refresh in that window would reconnect and revive the stopped run.
            if store.checkpointer is not None and run_id:
                await store.clear_active_run_id(session_id, expected=run_id)
            return {"ok": True}
        # No live run: still let the user clear a stranded interrupted run so a
        # page reload doesn't trigger an unwanted reconnect.
        if store.checkpointer is not None:
            run_id = await store.get_active_run_id(session_id)
            if run_id:
                await store.checkpointer.delete(run_id)
            await store.clear_active_run_id(session_id)
            return {"ok": True}
        raise HTTPException(status_code=404, detail="no active stream")

    @router.post("/api/chat/reconnect")
    async def chat_reconnect(session_id: str = Query(...)) -> EventSourceResponse:
        """Re-attach to a live run, or resume an interrupted one from checkpoint.

        Called by the frontend after a page refresh when the session endpoint
        returns a non-null ``active_run_id``.
        """
        live = deps.supervisor.get(session_id)
        if live is not None:
            return EventSourceResponse(
                forward(
                    live.attach(with_snapshot=True),
                    sid=session_id,
                    emit_session=True,
                )
            )

        if store.checkpointer is None:
            raise HTTPException(status_code=404, detail="no checkpointer configured")
        run_id = await store.get_active_run_id(session_id)
        if run_id is None:
            raise HTTPException(status_code=404, detail="no interrupted run")
        snapshot = await store.checkpointer.load(run_id)
        if snapshot is None or snapshot.status not in ("interrupted", "running"):
            await store.clear_active_run_id(session_id)
            raise HTTPException(status_code=404, detail="no resumable run")
        if snapshot.agent_name not in deps.agents:
            await store.checkpointer.delete(run_id)
            await store.clear_active_run_id(session_id)
            raise HTTPException(
                status_code=409,
                detail=f"agent {snapshot.agent_name!r} is no longer registered",
            )

        try:
            ctrl = await deps.supervisor.start_resume(
                session_id=session_id,
                agent=deps.agents[snapshot.agent_name],
                snapshot=snapshot,
            )
        except HTTPException as exc:
            # Lost a concurrent-reconnect race: attach to the winner's run.
            live = deps.supervisor.get(session_id)
            if exc.status_code == 409 and live is not None:
                return EventSourceResponse(
                    forward(
                        live.attach(with_snapshot=True),
                        sid=session_id,
                        emit_session=True,
                    )
                )
            raise
        return EventSourceResponse(
            forward(ctrl.subscribe_live(), sid=session_id, emit_session=True)
        )

    return router
