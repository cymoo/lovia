"""Chat routes: blocking turn, SSE stream, approval, cancel, and reconnect."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Query, Request
    from sse_starlette.sse import EventSourceResponse
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ... import events
from ...checkpointer import CheckpointOptions
from ...messages import Message
from ...reliability import CancelToken
from ...runner import Runner
from ...runtime.result import RunHandle
from ...steering import Mailbox
from ..schemas import (
    ApprovalRequest,
    ChatRequest,
    ChatResponse,
    InjectCancelRequest,
    InjectRequest,
)
from ..sse import _coerce, event_to_sse, usage_dict
from ..titles import provisional_title
from .deps import RouterDeps

log = logging.getLogger(__name__)


@dataclass
class _DriveResult:
    """Out-parameter for :func:`drive_stream` (a generator can't also return)."""

    succeeded: bool = False
    final_output: Any = None


async def drive_stream(
    handle: RunHandle,
    *,
    sid: str,
    request: Request,
    cancel: CancelToken,
    deps: RouterDeps,
    out: _DriveResult,
    emit_session: bool = True,
) -> AsyncIterator[dict[str, str]]:
    """Drive a Runner stream to SSE payloads, recording the result in ``out``.

    Lifecycle-free: the caller owns the per-session cancel token, mailbox, and
    checkpoint pointer and tears them down once the whole (possibly
    auto-chained) connection is finished. ``emit_session`` is False for chained
    runs so the ``session`` envelope is sent only once per connection.
    """
    if emit_session:
        # Tell the client its session id up front so reconnects work.
        yield {"event": "session", "data": json.dumps({"session_id": sid})}
    error_seen = False
    try:
        async for ev in handle:
            if await request.is_disconnected():
                cancel.cancel("client disconnected")
                break
            approval_ev = ev if isinstance(ev, events.ApprovalRequired) else None
            if approval_ev is not None:
                deps.approvals.register(sid, approval_ev)
            payload = event_to_sse(ev)
            if payload is not None:
                if payload["event"] == "error":
                    error_seen = True
                yield payload
            if isinstance(ev, events.RunCompleted):
                out.succeeded = True
                out.final_output = ev.result.output
            if approval_ev is not None:
                await deps.approvals.await_decision(sid, approval_ev)
    except Exception as exc:
        # Fatal run errors (MaxTurnsExceeded, provider failure, …) are usually
        # surfaced to the client as an `error` event by the loop before it
        # re-raises. If we reach here without having forwarded one (e.g. a
        # failure in the SSE layer itself), emit a terminal `error` so the
        # client shows a clear notice instead of a silently truncated reply.
        # Either way, swallow the re-raise so the stream closes cleanly rather
        # than faulting the ASGI response.
        log.warning("stream %s ended with error: %s", sid, exc)
        if not error_seen:
            yield {
                "event": "error",
                "data": json.dumps({"type": type(exc).__name__, "message": str(exc)}),
            }
    finally:
        await deps.approvals.release(sid)


def build_chat_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()
    store = deps.store
    session = deps.session

    async def _checkpoint_for(sid: str, run_id: str) -> CheckpointOptions | None:
        """Register ``run_id`` as the session's active run and build its
        checkpoint options; ``None`` when no checkpointer is configured."""
        if store.checkpointer is None:
            return None
        await store.set_active_run_id(sid, run_id)
        return CheckpointOptions(
            checkpointer=store.checkpointer,
            run_id=run_id,
            delete_on_success=True,
        )

    @router.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        agent = deps.pick(req.agent)
        sid = req.session_id or uuid.uuid4().hex
        is_new = (await store.get(sid)) is None
        await store.upsert(
            sid,
            agent=agent.name,
            title=provisional_title(req.message) if is_new else None,
        )
        result = await Runner.run(
            agent,
            req.message,
            session=session,
            session_id=sid,
            context_policy=deps.context_policy,
            max_turns=deps.max_turns,
            budget=deps.budget,
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
    async def chat_stream(req: ChatRequest, request: Request) -> EventSourceResponse:
        agent = deps.pick(req.agent)
        sid = req.session_id or uuid.uuid4().hex
        is_new = (await store.get(sid)) is None
        await store.upsert(
            sid,
            agent=agent.name,
            title=provisional_title(req.message) if is_new else None,
        )

        # A new message always starts a fresh run. Delete any checkpoint left by
        # a previous interrupted run so the reconnect endpoint won't pick up a
        # stale snapshot for this session.
        if store.checkpointer is not None:
            old_run_id = await store.get_active_run_id(sid)
            if old_run_id:
                await store.checkpointer.delete(old_run_id)

        # Cancel any previous stream on this session.
        if sid in deps.cancel_tokens:
            deps.cancel_tokens[sid].cancel("new stream started")

        cancel = CancelToken()
        deps.cancel_tokens[sid] = cancel
        mailbox = Mailbox()
        deps.mailboxes[sid] = mailbox

        checkpoint_opts = await _checkpoint_for(sid, uuid.uuid4().hex)

        async def gen() -> AsyncIterator[dict[str, str]]:
            first = True
            title_args: tuple[str, str, Any, str] | None = None
            next_input: str | list[Message] = req.message
            cur_ckpt = checkpoint_opts
            out = _DriveResult()
            try:
                while True:
                    handle = Runner.stream(
                        agent,
                        next_input,
                        session=session,
                        session_id=sid,
                        context_policy=deps.context_policy,
                        cancel_token=cancel,
                        mailbox=mailbox,
                        max_turns=deps.max_turns,
                        budget=deps.budget,
                        retry=deps.retry,
                        tracer=deps.tracer,
                        checkpoint=cur_ckpt,
                    )
                    out = _DriveResult()
                    async for payload in drive_stream(
                        handle,
                        sid=sid,
                        request=request,
                        cancel=cancel,
                        deps=deps,
                        out=out,
                        emit_session=first,
                    ):
                        yield payload
                    if first and is_new and out.succeeded:
                        title_args = (sid, req.message, out.final_output, agent.name)
                    first = False

                    # Anything still queued arrived too late to be drained at a
                    # turn start. Re-queue it and start the next run with empty
                    # input over this same stream: the next run drains it at its
                    # first turn, so it emits `user_injected` (rendered as a user
                    # turn) instead of silently folding into the run input.
                    leftover = mailbox.drain()
                    if not leftover or cancel.is_cancelled or not out.succeeded:
                        break
                    for content in leftover:
                        mailbox.push(content)
                    next_input = []
                    cur_ckpt = await _checkpoint_for(sid, uuid.uuid4().hex)
            finally:
                deps.cancel_tokens.pop(sid, None)
                deps.mailboxes.pop(sid, None)
                # On success the checkpoint was already deleted
                # (delete_on_success); clear the DB pointer so reconnect finds
                # nothing.
                if out.succeeded and store.checkpointer is not None:
                    await store.clear_active_run_id(sid)
            if title_args is not None:
                deps.schedule_title(*title_args)

        return EventSourceResponse(gen())

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
        mailbox = deps.mailboxes.get(req.session_id)
        if mailbox is None:
            return {"accepted": False}
        return {"accepted": True, "id": mailbox.push(message)}

    @router.post("/api/chat/uninject")
    async def chat_uninject(req: InjectCancelRequest) -> dict[str, bool]:
        """Withdraw a still-queued message before the run drains it.

        ``{"removed": false}`` if it was already consumed or no run is active.
        """
        mailbox = deps.mailboxes.get(req.session_id)
        removed = mailbox.remove(req.id) if mailbox is not None else False
        return {"removed": removed}

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
        """Cancel an in-progress stream for ``session_id``."""
        token = deps.cancel_tokens.get(session_id)
        if token is None:
            raise HTTPException(status_code=404, detail="no active stream")
        token.cancel("user requested stop")
        # User explicitly stopped: delete the checkpoint and clear the pointer so
        # a subsequent page reload doesn't trigger an unwanted reconnect.
        if store.checkpointer is not None:
            run_id = await store.get_active_run_id(session_id)
            if run_id:
                await store.checkpointer.delete(run_id)
            await store.clear_active_run_id(session_id)
        return {"ok": True}

    @router.post("/api/chat/reconnect")
    async def chat_reconnect(
        request: Request, session_id: str = Query(...)
    ) -> EventSourceResponse:
        """Resume an interrupted run for ``session_id``.

        Called automatically by the frontend after a page refresh when the
        session endpoint returns a non-null ``active_run_id``. The resumed run
        picks up from the last checkpoint turn; no new user input is needed.
        """
        if store.checkpointer is None:
            raise HTTPException(status_code=404, detail="no checkpointer configured")

        run_id = await store.get_active_run_id(session_id)
        if run_id is None:
            raise HTTPException(status_code=404, detail="no interrupted run")

        snapshot = await store.checkpointer.load(run_id)
        if snapshot is None or snapshot.status not in ("interrupted", "running"):
            await store.clear_active_run_id(session_id)
            raise HTTPException(status_code=404, detail="no resumable run")

        agent_name = snapshot.agent_name
        if agent_name not in deps.agents:
            await store.checkpointer.delete(run_id)
            await store.clear_active_run_id(session_id)
            raise HTTPException(
                status_code=409,
                detail=f"agent {agent_name!r} is no longer registered",
            )

        agent = deps.agents[agent_name]

        if session_id in deps.cancel_tokens:
            raise HTTPException(
                status_code=409, detail="a stream is already in progress"
            )

        cancel = CancelToken()
        deps.cancel_tokens[session_id] = cancel
        mailbox = Mailbox()
        deps.mailboxes[session_id] = mailbox

        # Pass the pre-loaded snapshot directly to avoid a race between our check
        # above and the Runner's own checkpointer lookup.
        checkpoint_opts = CheckpointOptions(
            checkpointer=store.checkpointer,
            resume_from=snapshot,
            delete_on_success=True,
        )

        async def gen() -> AsyncIterator[dict[str, str]]:
            out = _DriveResult()
            try:
                handle = Runner.stream(
                    agent,
                    [],  # input is ignored on resume; transcript already has it
                    session=session,
                    session_id=session_id,
                    context_policy=deps.context_policy,
                    cancel_token=cancel,
                    mailbox=mailbox,
                    max_turns=deps.max_turns,
                    budget=deps.budget,
                    retry=deps.retry,
                    tracer=deps.tracer,
                    checkpoint=checkpoint_opts,
                )
                async for payload in drive_stream(
                    handle,
                    sid=session_id,
                    request=request,
                    cancel=cancel,
                    deps=deps,
                    out=out,
                ):
                    yield payload
            finally:
                deps.cancel_tokens.pop(session_id, None)
                deps.mailboxes.pop(session_id, None)
                if out.succeeded and store.checkpointer is not None:
                    await store.clear_active_run_id(session_id)

        return EventSourceResponse(gen())

    return router
