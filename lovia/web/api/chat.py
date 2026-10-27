"""Chat routes: blocking turn, SSE stream/attach, approval, cancel, reconnect.

Every chat turn is a run owned by the
:class:`~lovia.web.supervisor.RunSupervisor`, not the request:
``/chat/stream`` starts (or attaches to) one and forwards its event hub to
SSE, ``/chat`` starts one and answers with its outcome. A disconnect
detaches; the run keeps going.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

try:
    from fastapi import APIRouter, Query, Request
    from sse_starlette.sse import EventSourceResponse
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...agent import Agent
from ..attachments import build_user_input
from ..errors import WebError
from ..run_deps import RunTrigger, build_run_deps
from ..schemas import (
    AnswerRequest,
    ApprovalRequest,
    ChatRequest,
    ChatResponse,
    InjectCancelRequest,
    InjectRequest,
)
from ..sse import SessionCreatedData, _coerce, usage_dict
from ..store import ChatMeta
from ..supervisor import RunController, forward
from ..titles import provisional_title
from .deps import RouterDeps
from .serialization import Run, segments_runs


def _busy() -> WebError:
    """The refusal of a ``busy="reject"`` message for a chat with a live run."""
    return WebError(
        409,
        "run_active",
        "a run is active for this session",
        hint='sent with busy="reject": send it again once that run has ended',
    )


def build_chat_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()
    store = deps.store
    session = deps.session

    async def upsert_session(
        sid: str, agent_name: str, message: str, *, is_new: bool
    ) -> None:
        """Insert/touch the session's metadata row (announcing a new chat)."""
        title = provisional_title(message) if is_new else None
        await store.upsert(sid, agent=agent_name, title=title)
        if is_new:
            deps.emit(
                "session_created",
                SessionCreatedData(session_id=sid, agent=agent_name, title=title),
            )

    def resolve_agent(meta: ChatMeta | None, requested: str | None) -> Agent[Any]:
        """The agent that runs this turn, given the session's metadata (if any).

        An existing session keeps the agent it was created with — a stale tab
        (or a switcher left on another agent) must not silently swap a chat's
        brain mid-conversation. ``requested`` applies to new sessions, and as
        the fallback when the stored agent is no longer served. Side benefit:
        continuing an existing session needs no ``agent`` field at all.
        """
        if meta is not None and meta.agent and meta.agent in deps.agents:
            return deps.agents[meta.agent]
        return deps.pick(requested)

    @router.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, request: Request) -> ChatResponse:
        # Blocking turn: a supervised run, as for a stream, that this request
        # waits out. The run holds the chat and answers to a stop, a deletion
        # and shutdown; a disconnect only detaches.
        if not req.message.strip() and not req.attachments:
            raise WebError(422, "invalid_request", "empty message")
        sid = req.session_id or uuid.uuid4().hex
        meta = await store.get(sid)  # one read feeds agent choice AND is_new
        agent = resolve_agent(meta, req.agent)
        is_new = meta is None
        if deps.supervisor.get(sid) is not None:
            # Unlike a stream, this turn can't join the live run: it would
            # have no answer of its own to return.
            raise WebError(
                409,
                "run_active",
                "a run is active for this session",
                hint="use /api/chat/stream to attach or inject",
            )
        user_input = build_user_input(req, agent)
        if isinstance(user_input, str) and not user_input.strip():
            # Attachments were given but all invalid (missing / outside the
            # workspace) and there's no text — nothing to run.
            raise WebError(422, "invalid_request", "empty message")
        client = req.context or {}
        # The run_deps hook decides first, before anything is written: a
        # refused message leaves no trace.
        trigger = RunTrigger(
            session_id=sid,
            agent=agent,
            source="user",
            request=request,
            client_context=client,
        )
        run_deps = await build_run_deps(deps.run_deps, trigger)
        await upsert_session(sid, deps.name_of(agent), req.message, is_new=is_new)
        # The chat's interrupted run, superseded by this one: read before the
        # start, deleted only after it, so a refused start (409/429) leaves it
        # resumable.
        checkpointer = store.checkpointer
        stranded = (
            await store.get_active_run_id(sid) if checkpointer is not None else None
        )
        ctrl = await deps.supervisor.start(
            session_id=sid,
            agent=agent,
            input=user_input,
            is_new=is_new,
            title_message=req.message,
            autostart=True,
            client=client,
            run_deps=run_deps,
            # No client to ask: Runner.run's approval behaviour.
            await_approvals=False,
        )
        assert ctrl.task is not None  # autostart began it
        if checkpointer is not None and stranded:
            await checkpointer.delete(stranded)
        # asyncio.wait never cancels what it waits on: a request cancelled
        # by its client going away leaves the run to finish into the chat.
        await asyncio.wait([ctrl.task])
        # An exception after a completed leg (an auto-chain hop that failed)
        # is a failure — the stream's error event says so too.
        if ctrl.final_status == "completed" and ctrl.final_exception is None:
            return ChatResponse(
                output=_coerce(ctrl.final_output),
                session_id=sid,
                usage=usage_dict(ctrl.usage, last_input_tokens=ctrl.last_input_tokens),
            )
        if ctrl.cancel.is_cancelled or ctrl.final_exception is None:
            # A stop: /api/chat/cancel, the chat's deletion, or shutdown.
            raise WebError(409, "run_cancelled", "the run was stopped")
        raise WebError.from_exc(500, "run_failed", ctrl.final_exception)

    @router.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest, request: Request) -> EventSourceResponse:
        sid = req.session_id or uuid.uuid4().hex
        if req.busy == "reject" and deps.supervisor.get(sid) is not None:
            # Known busy on arrival: refused before the hook or any write. A
            # run that starts later is caught in attach() — no inject, no
            # start, though the hook may have run and the chat been touched.
            raise _busy()
        meta = await store.get(sid)  # one read feeds agent choice AND is_new
        agent = resolve_agent(meta, req.agent)
        is_new = meta is None
        # Build the turn input once — this validates attachments (dropping any
        # that are missing or escape the workspace), so an attachments-only
        # request whose attachments are all invalid reads as empty here.
        user_input = build_user_input(req, agent)
        empty = isinstance(user_input, str) and not user_input.strip()
        # An empty turn is only meaningful as a pure attach to a live run;
        # rejecting it otherwise before the upsert avoids littering empty rows.
        if empty and deps.supervisor.get(sid) is None:
            raise WebError(422, "invalid_request", "empty message")
        client = req.context or {}
        trigger = RunTrigger(
            session_id=sid,
            agent=agent,
            source="user",
            request=request,
            client_context=client,
        )
        # A message for a live run joins it, in that run's context. Any other
        # starts one: the run_deps hook decides first, before anything is
        # written, so a refused message leaves no trace.
        decided = deps.supervisor.get(sid) is None
        run_deps = None
        if decided:
            run_deps = await build_run_deps(deps.run_deps, trigger)
        await upsert_session(sid, deps.name_of(agent), req.message, is_new=is_new)

        def attach(live: RunController) -> EventSourceResponse:
            # A run is already live for this session: a new message injects
            # (Phase 1); this connection attaches to co-watch it. Every path
            # that finds one comes through here, so busy="reject" holds on
            # each — a message never meant to steer is refused instead.
            if req.busy == "reject":
                raise _busy()
            if req.message.strip():
                live.inject(req.message)
            return EventSourceResponse(
                forward(live.attach(with_snapshot=True), sid=sid, run_id=live.record_id)
            )

        live = deps.supervisor.get(sid)
        if live is not None:
            return attach(live)
        if empty:
            # The live run we would have attached to ended mid-request.
            raise WebError(422, "invalid_request", "empty message")
        if not decided:  # ... and so did the one this message was to join
            run_deps = await build_run_deps(deps.run_deps, trigger)
            # Another may have started while the hook ran: join that one,
            # before the cleanup below could take its checkpoint for stale.
            if (live := deps.supervisor.get(sid)) is not None:
                return attach(live)

        # No live run → start a fresh supervised run. The chat's interrupted
        # run, superseded by it, is read before the start and deleted only
        # after it (as in /chat), so a refused start (409/429) leaves it
        # resumable.
        checkpointer = store.checkpointer
        stranded = (
            await store.get_active_run_id(sid) if checkpointer is not None else None
        )
        try:
            ctrl = await deps.supervisor.start(
                session_id=sid,
                agent=agent,
                input=user_input,
                is_new=is_new,
                title_message=req.message,
                client=client,
                run_deps=run_deps,
            )
        except WebError as exc:
            # Lost a concurrent-start race (two tabs submitting at once): the
            # winner owns the run, so deliver this message by injecting into it.
            live = deps.supervisor.get(sid)
            if exc.code == "run_active" and live is not None:
                return attach(live)
            raise
        # Nothing may await between the start and the subscribe that begins
        # the run; past it, a failed delete only orphans a checkpoint.
        sub = ctrl.subscribe_live()
        if checkpointer is not None and stranded:
            await checkpointer.delete(stranded)
        return EventSourceResponse(forward(sub, sid=sid, run_id=ctrl.record_id))

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
            raise WebError(422, "invalid_request", "empty message")
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
            raise WebError(404, "approval_not_found", "no pending approval matches")
        return {"ok": True}

    @router.post("/api/chat/answer")
    async def answer_question(req: AnswerRequest) -> dict[str, bool]:
        """Answer the session's pending ``ask_human`` question.

        404 when no question channel is configured, or when the question has
        already been answered/cancelled (e.g. it timed out) — the client shows
        the card as expired and the tool-error result follows in the stream.
        """
        if deps.questions is None:
            raise WebError(404, "feature_unavailable", "no question channel")
        if not deps.questions.resolve(req.session_id, req.answer):
            raise WebError(404, "question_not_found", "no pending question matches")
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
        raise WebError(404, "run_not_found", "no active stream")

    @router.post("/api/chat/reconnect")
    async def chat_reconnect(
        request: Request, session_id: str = Query(...)
    ) -> EventSourceResponse:
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
                    run_id=live.record_id,
                )
            )

        if store.checkpointer is None:
            raise WebError(404, "feature_unavailable", "no checkpointer configured")
        run_id = await store.get_active_run_id(session_id)
        if run_id is None:
            raise WebError(404, "run_not_found", "no interrupted run")
        snapshot = await store.checkpointer.load(run_id)
        if snapshot is None or snapshot.status not in ("interrupted", "running"):
            await store.clear_active_run_id(session_id)
            raise WebError(404, "run_not_found", "no resumable run")
        if snapshot.agent_name not in deps.agents:
            await store.checkpointer.delete(run_id)
            await store.clear_active_run_id(session_id)
            raise WebError(
                409,
                "agent_unregistered",
                f"agent {snapshot.agent_name!r} is no longer registered",
            )
        agent = deps.agents[snapshot.agent_name]
        # The run's own client context (a checkpoint keeps none), with the
        # reconnecting request for identity.
        client = await store.get_active_context(session_id) or {}
        run_deps = await build_run_deps(
            deps.run_deps,
            RunTrigger(
                session_id=session_id,
                agent=agent,
                source="user",
                request=request,
                client_context=client,
                resumed=True,
            ),
        )
        # Another reconnect may have resumed the run while the hook ran, even
        # to its end: resume only what the chat still points at, read afresh.
        if await store.get_active_run_id(session_id) != run_id:
            raise WebError(404, "run_not_found", "no resumable run")
        snapshot = await store.checkpointer.load(run_id)
        if snapshot is None or snapshot.status not in ("interrupted", "running"):
            raise WebError(404, "run_not_found", "no resumable run")

        # Built BEFORE start_resume: that call registers the controller, and
        # nothing may await between registration and the subscribe that begins
        # its task. An await there could be cancelled (client disconnect) or
        # raise, stranding a registered controller that never starts — later
        # reconnects would attach to a hub that publishes nothing and hang.
        #
        # Same splice `_session_view` used to build the view the client is
        # looking at right now (session history + the checkpoint's entries), so
        # the snapshot it re-renders matches rather than fights it.
        runs = [
            *segments_runs(await session.segments(session_id)),
            Run(None, list(snapshot.entries)),  # named by its refs frame once done
        ]

        try:
            ctrl = await deps.supervisor.start_resume(
                session_id=session_id,
                agent=agent,
                snapshot=snapshot,
                client=client,
                run_deps=run_deps,
            )
        except WebError as exc:
            # Lost a concurrent-reconnect race: attach to the winner's run.
            live = deps.supervisor.get(session_id)
            if exc.code == "run_active" and live is not None:
                return EventSourceResponse(
                    forward(
                        live.attach(with_snapshot=True),
                        sid=session_id,
                        run_id=live.record_id,
                    )
                )
            raise
        return EventSourceResponse(
            forward(ctrl.subscribe_resume(runs), sid=session_id, run_id=ctrl.record_id)
        )

    return router
