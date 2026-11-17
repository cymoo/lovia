"""Chat routes: blocking turn, SSE stream/attach, approval, cancel, reconnect.

Every chat turn is a run owned by the
:class:`~lovia.web.supervisor.RunSupervisor`, not the request:
``/chat/stream`` starts (or attaches to) one and forwards its event hub to
SSE, ``/chat`` starts one and answers with its outcome. A disconnect
detaches; the run keeps going.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from typing import Any

try:
    from fastapi import APIRouter, Query, Request
    from sse_starlette.sse import EventSourceResponse
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...agent import Agent
from ...messages import user
from ..attachments import build_user_input, user_content
from ..errors import WebError
from ..run_deps import RunTrigger, build_run_deps
from ...approvals import Approval
from ..schemas import (
    AnswerRequest,
    ApprovalRequest,
    ApprovalResponse,
    ChatRequest,
    ChatResponse,
    InjectCancelRequest,
    InjectRequest,
)
from ..sse import SessionCreatedData, _coerce, usage_dict
from ..store import ChatMeta
from ..supervisor import RunController, forward
from ..titles import provisional_title
from ..waiting import (
    RESUMABLE,
    Waiting,
    find_waiting,
    resume_waiting,
    stop_waiting,
)
from ..workspaces import ChatBinding, own_root
from .deps import RouterDeps
from .serialization import Run, segments_runs

log = logging.getLogger(__name__)


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
        sid: str, agent: Agent[Any], req: ChatRequest, binding: ChatBinding | None
    ) -> None:
        """Insert/touch the session's metadata row (announcing a new chat).

        A new chat (``binding`` given) records where and how it works — the
        directory and the mode the page picked — which its runs then read
        (supervisor.start); its directory joins the workspace list.
        """
        agent_name = deps.name_of(agent)
        is_new = binding is not None
        title = provisional_title(req.message) if is_new else None
        await store.upsert(
            sid,
            agent=agent_name,
            title=title,
            workspace_root=binding.root if binding else None,
            workspace_mode=binding.mode if binding else None,
            model=req.model if is_new else None,
            unarchive=True,  # the user is back in it
        )
        if binding is not None:
            if deps.workspace_switch is not None and binding.root is not None:
                await store.touch_workspace(binding.root)
            deps.emit(
                "session_created",
                SessionCreatedData(session_id=sid, agent=agent_name, title=title),
            )

    def bind(agent: Agent[Any], meta: ChatMeta | None, req: ChatRequest) -> ChatBinding:
        """Where and how this turn's chat works — checked now, before the
        message is read (its attachments live in that directory) or anything
        is written. A new chat takes the request's directory, mode and
        model, which must be ones this server offers; an existing one its
        own."""
        switch = deps.workspace_switch
        if meta is not None:
            if (
                meta.workspace_root is None
                and switch is not None
                and switch.roots is not None
                and own_root(agent) is not None
            ):
                # From before chats had a directory, on a server where they
                # may work elsewhere: which one is the person's call — never
                # a guess. Clientless runs (schedules, task reports) carry
                # on in the agent's own, as they always did.
                raise WebError(
                    409,
                    "workspace_unassociated",
                    "this chat has no directory yet",
                    hint="pick one: PATCH /api/sessions/{id} with workspace_root, "
                    "or POST /api/sessions/associate",
                )
            return deps.chat_binding(agent, meta)
        if req.model is not None:
            deps.check_chat_model(req.model)
        mode = req.workspace_mode or (switch.default_mode if switch else None)
        return deps.requested_binding(agent, mode, req.workspace)

    def scope(
        agent: Agent[Any], meta: ChatMeta | None, req: ChatRequest, binding: ChatBinding
    ) -> Agent[Any]:
        """``agent`` as the chat has it before a run binds the rest: in the
        chat's directory and on the chat's model — which decides whether an
        attached image is sent as pixels — and what a ``run_deps`` hook is
        shown."""
        model = meta.model if meta is not None else req.model
        return deps.on_chat_model(deps.at_root(agent, binding), model)

    def resolve_agent(meta: ChatMeta | None, requested: str | None) -> Agent[Any]:
        """The agent that runs this turn, given the session's metadata (if any).

        An existing session keeps the agent it was created with — a stale tab
        (or a switcher left on another agent) must not silently swap a chat's
        brain mid-conversation — and runs on the default one once that agent
        is no longer served. ``requested`` applies to new sessions. Side
        benefit: continuing an existing session needs no ``agent`` field.
        """
        if meta is not None:
            return deps.pick(deps.agent_of(meta))
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
        binding = bind(agent, meta, req)
        if deps.supervisor.get(sid) is not None:
            # Unlike a stream, this turn can't join the live run: it would
            # have no answer of its own to return.
            raise WebError(
                409,
                "run_active",
                "a run is active for this session",
                hint="use /api/chat/stream to attach or inject",
            )
        if await find_waiting(deps, sid) is not None:
            # Nor can it resume one suspended on a card: that waits on a person.
            raise WebError(
                409,
                "run_waiting",
                "this chat's run waits on an approval",
                hint="answer its approval card, or send with /api/chat/stream",
            )
        scoped = scope(agent, meta, req, binding)
        user_input = await build_user_input(req, scoped)
        if isinstance(user_input, str) and not user_input.strip():
            # Attachments were given but all invalid (missing / outside the
            # workspace) and there's no text — nothing to run.
            raise WebError(422, "invalid_request", "empty message")
        client = req.context or {}
        # The run_deps hook decides first, before anything is written: a
        # refused message leaves no trace.
        trigger = RunTrigger(
            session_id=sid,
            agent=scoped,
            source="user",
            request=request,
            client_context=client,
        )
        run_deps = await build_run_deps(deps.run_deps, trigger)
        await upsert_session(sid, agent, req, binding if is_new else None)
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
        binding = bind(agent, meta, req)
        scoped = scope(agent, meta, req, binding)
        # Build the message once — this validates attachments (dropping any
        # that are missing or escape the chat's directory), so an
        # attachments-only request whose attachments are all invalid reads as
        # empty here. It starts a run, or steers the live one.
        content = await user_content(req.message, req.attachments, scoped)
        empty = isinstance(content, str) and not content.strip()
        # An empty turn is only meaningful as a pure attach to a live run;
        # rejecting it otherwise before the upsert avoids littering empty rows.
        if empty and deps.supervisor.get(sid) is None:
            raise WebError(422, "invalid_request", "empty message")
        client = req.context or {}
        trigger = RunTrigger(
            session_id=sid,
            agent=scoped,
            source="user",
            request=request,
            client_context=client,
        )
        # A run suspended on a card is still the chat's run: a message joins
        # it as one joins a live run (resuming it), never starts another.
        waiting = (
            await find_waiting(deps, sid) if deps.supervisor.get(sid) is None else None
        )
        if waiting is not None and req.busy == "reject":
            raise WebError(
                409,
                "run_waiting",
                "this chat's run waits on an approval",
                hint='sent with busy="reject": send it again once that run has ended',
            )
        # A message for a live run joins it, in that run's context. Any other
        # starts one: the run_deps hook decides first, before anything is
        # written, so a refused message leaves no trace.
        decided = deps.supervisor.get(sid) is None and waiting is None
        run_deps = None
        if decided:
            run_deps = await build_run_deps(deps.run_deps, trigger)
        await upsert_session(sid, agent, req, binding if is_new else None)

        def attach(live: RunController) -> EventSourceResponse:
            # A run is already live for this session: a new message injects
            # (Phase 1); this connection attaches to co-watch it. Every path
            # that finds one comes through here, so busy="reject" holds on
            # each — a message never meant to steer is refused instead.
            if req.busy == "reject":
                raise _busy()
            if not empty:
                live.inject(content)
            return EventSourceResponse(
                forward(live.attach(with_snapshot=True), sid=sid, run_id=live.record_id)
            )

        live = deps.supervisor.get(sid)
        if live is not None:
            return attach(live)
        if empty:
            # The live run we would have attached to ended mid-request.
            raise WebError(422, "invalid_request", "empty message")
        if waiting is not None:
            # Resumed with the message in its mailbox: it asks its card again,
            # live, and reads the message once the card is answered.
            resumed = await resume_waiting(
                deps, waiting, message=content, request=request
            )
            if resumed is None:  # another request took it first: join that
                if (live := deps.supervisor.get(sid)) is not None:
                    return attach(live)
                raise WebError(404, "run_not_found", "the waiting run was stopped")
            return EventSourceResponse(
                forward(
                    resumed.attach(with_snapshot=True),
                    sid=sid,
                    run_id=resumed.record_id,
                )
            )
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
                input=content if isinstance(content, str) else [user(content)],
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
        """Queue a message, with any attachments, into the active run for ``session_id``.

        ``{"accepted": true, "id": <token>}`` when a run is live (drained at the
        next turn start; the token withdraws it via ``/uninject``).
        ``{"accepted": false}`` when no run is active — the "run just ended" race —
        so the client can fall back to a normal stream without losing the message.
        """
        message = req.message.strip()
        if not message and not req.attachments:
            raise WebError(422, "invalid_request", "empty message")
        ctrl = deps.supervisor.get(req.session_id)
        if ctrl is None:
            return {"accepted": False}
        content = await user_content(message, req.attachments, ctrl.agent)
        if not content:  # attachments only, and none of them usable
            raise WebError(422, "invalid_request", "empty message")
        if deps.supervisor.get(req.session_id) is not ctrl:
            # The run ended while its attachments were being looked up (on
            # another machine, for a remote chat): nothing left to steer.
            return {"accepted": False}
        return {"accepted": True, "id": ctrl.inject(content)}

    @router.post("/api/chat/uninject")
    async def chat_uninject(req: InjectCancelRequest) -> dict[str, bool]:
        """Withdraw a still-queued message before the run drains it.

        ``{"removed": false}`` if it was already consumed or no run is active.
        """
        ctrl = deps.supervisor.get(req.session_id)
        return {"removed": bool(ctrl is not None and ctrl.uninject(req.id))}

    @router.post("/api/chat/approve", response_model=ApprovalResponse)
    async def approve(req: ApprovalRequest, request: Request) -> ApprovalResponse:
        """Decide a pending approval; optionally remember an approval.

        Claim → (write the grant) → resolve, in that order: the runner wakes
        only once the grant is on disk, so the next call in the same scope
        can't ask again meanwhile. The claim makes this request the decision's
        owner — a second request for the same call gets 404, and the timeout /
        cancel sweeps leave it alone — and the ``finally`` honours the user's
        decision even if saving the grant fails (then ``remembered`` is false).

        ``scopes`` narrows what is remembered to some of the request's own —
        checked before the claim, so a malformed request decides nothing.

        A card whose run was suspended waiting for it resumes that run with
        the answer (``resumed``), with no client attached: a page follows it
        with ``POST /api/chat/reconnect``.
        """
        sid, cid = req.session_id, req.call_id
        pending = deps.approvals.pending(sid, cid)
        if pending is None:
            waiting = await find_waiting(deps, sid)
            if waiting is not None and waiting.call_id == cid:
                return await _answer_waiting(waiting, req, request)
        _check_scopes(req.scopes, pending.scopes if pending is not None else None)
        if not deps.approvals.claim(sid, cid):
            raise WebError(404, "approval_not_found", "no pending approval matches")
        approved = req.decision == "approve"
        remembered = False
        try:
            if approved and req.remember and pending is not None:
                remembered = await _remember(sid, pending.scopes, req.scopes)
        finally:
            await deps.approvals.resolve(sid, cid, Approval(approved, req.reason))
        return ApprovalResponse(ok=True, remembered=remembered)

    async def _answer_waiting(
        waiting: Waiting, req: ApprovalRequest, request: Request
    ) -> ApprovalResponse:
        offered = waiting.scopes
        _check_scopes(req.scopes, offered)
        approved = req.decision == "approve"
        ctrl = await resume_waiting(
            deps, waiting, answer=Approval(approved, req.reason), request=request
        )
        if ctrl is None:  # another tab's answer, or a stop, took it first
            raise WebError(404, "approval_not_found", "no pending approval matches")
        # Saved once the run is under way: its first step is this answered
        # call, so a later call in the same scope still finds the grant.
        remembered = (
            await _remember(req.session_id, offered, req.scopes)
            if approved and req.remember
            else False
        )
        return ApprovalResponse(ok=True, remembered=remembered, resumed=True)

    def _check_scopes(
        picked: Sequence[str] | None, offered: Sequence[str] | None
    ) -> None:
        if (
            picked is not None
            and offered is not None
            and not set(picked) <= set(offered)
        ):
            raise WebError(
                400,
                "invalid_request",
                "scopes must be among the pending request's own",
                hint=f"offered: {', '.join(offered) or '(none)'}",
            )

    async def _remember(
        sid: str, offered: Sequence[str], picked: list[str] | None
    ) -> bool:
        # The scopes come from the pending request itself — the client may
        # only narrow them; the context from the run as it is bound right now.
        ctrl = deps.supervisor.get(sid)
        if ctrl is None:
            return False
        scopes = [s for s in offered if picked is None or s in picked]
        if not scopes:
            return False
        try:
            # On the chat whose approvals the run shares (a task's: its chat's).
            await deps.store.add_grants(
                ctrl.grant_owner[0], scopes, ctrl.grant_context()
            )
        except Exception as exc:  # noqa: BLE001 — the approval itself still stands
            log.warning("could not remember approval for %s: %s", sid, exc)
            return False
        return True

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
            # Answer once the stopped chat is saved: a reload after this shows it.
            await deps.supervisor.settle(session_id)
            return {"ok": True}
        # A run suspended on a card is stopped like a live one: the chat keeps
        # what it showed.
        if (waiting := await find_waiting(deps, session_id)) is not None:
            if await stop_waiting(deps, waiting):
                return {"ok": True}
            if deps.supervisor.get(session_id) is not None:
                # Resumed by an answer meanwhile: stop that run instead.
                deps.supervisor.cancel(session_id)
                await deps.supervisor.settle(session_id)
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
        # A run suspended on a card resumes when the card is answered, not
        # because a page opened the chat.
        if await find_waiting(deps, session_id) is not None:
            raise WebError(
                409,
                "run_waiting",
                "this chat's run waits on an approval",
                hint="answer its approval card to resume it",
            )
        # A run stopped a moment ago retires its pointer once saved: never
        # resume it from the window before that.
        await deps.supervisor.settle(session_id)
        run_id = await store.get_active_run_id(session_id)
        if run_id is None:
            raise WebError(404, "run_not_found", "no interrupted run")
        snapshot = await store.checkpointer.load(run_id)
        if snapshot is None or snapshot.status not in RESUMABLE:
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
        # Checked before the hook, like a fresh turn: a chat whose directory
        # this server no longer allows is refused, not resumed elsewhere.
        binding = deps.chat_binding(agent, await store.get(session_id))
        # The run's own client context (a checkpoint keeps none), with the
        # reconnecting request for identity.
        client = await store.get_active_context(session_id) or {}
        run_deps = await build_run_deps(
            deps.run_deps,
            RunTrigger(
                session_id=session_id,
                agent=deps.at_root(agent, binding),
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
        if snapshot is None or snapshot.status not in RESUMABLE:
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
