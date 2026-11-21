"""Session routes: list, fetch, rename, delete, rewind, fork, todos, and export."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from typing import Any, cast
from weakref import WeakValueDictionary

try:
    from fastapi import APIRouter, Query
    from fastapi.responses import JSONResponse, PlainTextResponse, Response
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...checkpointer import RunSnapshot
from ...parts import ImagePart
from ...plugins import todos_from_entries
from ...transcript import InputEntry, ToolResultEntry, entries_to_messages
from ...types import JsonObject
from ..errors import WebError
from ..followups import FollowupRequest
from ..sse import (
    SessionCreatedData,
    SessionRetitledData,
    approval_data,
)
from ..schemas import (
    ChatSessionInfo,
    FollowupsResponse,
    ForkRequest,
    ForkResponse,
    GrantInfo,
    MessageOut,
    RewindRequest,
    RewindResponse,
    RunInfo,
    WaitsOn,
    RunRecordInfo,
    SessionDetail,
    SessionOrigin,
    SessionPatch,
    TodoItemOut,
    TodosResponse,
)
from ...runtime.loop import pending_tool_calls
from ..supervisor import RunController, agent_grant_context
from ..titles import provisional_title
from ..waiting import RESUMABLE, find_waiting
from ..workspaces import ChatBinding
from .deps import RouterDeps
from .serialization import (
    Run,
    export_md,
    export_txt,
    message_to_json_dict,
    origin_kind,
    tool_result_flags,
    run_record,
    segments_runs,
    segments_to_out,
    session_info,
    user_turn_refs,
    view_messages,
)


def _waits_on(waiting_for: JsonObject | None) -> WaitsOn | None:
    """What a ``waiting`` run's record says it waits on (``RunInfo.waits_on``)."""
    waiting_for = waiting_for or {}
    request = waiting_for.get("request")
    call_id = request.get("id") if isinstance(request, dict) else None
    if not isinstance(call_id, str):
        return None
    kind = waiting_for.get("kind") or "approval"
    if kind not in ("approval", "question", "call"):
        return None
    return WaitsOn(kind=kind, id=call_id)


def _asks(arguments: str, question: str) -> bool:
    """Whether a call's raw JSON ``arguments`` ask ``question``."""
    try:
        args = json.loads(arguments)
    except ValueError:
        return False
    return isinstance(args, dict) and args.get("question") == question


def _in_flight(snapshot: RunSnapshot) -> Run:
    """A checkpointed run still going, as the last of a view's runs: no run_id,
    so its turns get no ref until it finishes (the stream's refs frame) — a ref
    never names a turn its run has not written yet."""
    return Run(None, list(snapshot.entries))


async def _session_view(
    deps: RouterDeps, session_id: str, *, created_at: float, updated_at: float
) -> tuple[list[MessageOut], str | None, JsonObject | None]:
    """Resolve the transcript view + reconnect pointer for one session, and
    the card a suspended run waits on.

    * Live supervised run → flat history spliced with the checkpoint's entries
      so far, advertising the run's id (even before its first checkpoint) so
      the client reconnects — attach then delivers the authoritative snapshot.
    * Run suspended on a card → the same splice, and the card instead of a
      pointer: answering it resumes the run, opening the chat must not.
    * No live run but a resumable checkpoint (restart recovery) → the same
      splice, advertising the stored pointer; a stale pointer is cleaned up.
    * Finished → rebuilt from segments so persisted per-run compaction notices
      replay (a live run surfaces compaction over SSE instead).
    """
    store, session = deps.store, deps.session
    # A run stopped a moment ago is past the live branch but may not be saved.
    await deps.supervisor.settle(session_id)

    live = deps.supervisor.get(session_id)
    if live is not None:
        runs = segments_runs(await session.segments(session_id))
        if store.checkpointer is not None and live.run_id is not None:
            snapshot = await store.checkpointer.load(live.run_id)
            if snapshot is not None and snapshot.status in RESUMABLE:
                runs.append(_in_flight(snapshot))
        view = view_messages(runs, created_at=created_at, updated_at=updated_at)
        return view, live.run_id, None

    if (waiting := await find_waiting(deps, session_id)) is not None:
        runs = segments_runs(await session.segments(session_id))
        runs.append(_in_flight(waiting.snapshot))
        view = view_messages(runs, created_at=created_at, updated_at=updated_at)
        return view, None, {**waiting.request, "kind": waiting.kind}

    candidate = (
        await store.get_active_run_id(session_id)
        if store.checkpointer is not None
        else None
    )
    if candidate and store.checkpointer is not None:
        snapshot = await store.checkpointer.load(candidate)
        if snapshot is not None and snapshot.status in RESUMABLE:
            runs = segments_runs(await session.segments(session_id))
            runs.append(_in_flight(snapshot))
            view = view_messages(runs, created_at=created_at, updated_at=updated_at)
            return view, candidate, None
        # Stale pointer — clean it up so a reload doesn't offer a dead reconnect.
        await store.clear_active_run_id(session_id)
        if snapshot is not None:
            await store.checkpointer.delete(candidate)

    segments = await session.segments(session_id)
    return (
        segments_to_out(segments, created_at=created_at, updated_at=updated_at),
        None,
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
        parent: str | None = Query(None, max_length=64),
        per_workspace: int | None = Query(None, ge=1, le=1000),
        archived: bool = Query(False),
    ) -> list[ChatSessionInfo]:
        if parent:
            # A chat's own subagent tasks — the topbar tasks popover.
            metas = await store.list_children(parent)
        elif q:
            metas = await store.search(q, limit=limit, offset=offset)
        else:
            # per_workspace: the sidebar's groups — each directory's most
            # recent chats, plus every pinned one. archived: the chats put
            # away instead of those in use (a search covers both).
            metas = await store.list(
                limit=limit,
                offset=offset,
                per_workspace=per_workspace,
                archived=archived,
            )
        # Task sessions carry their latest run outcome so the topbar Tasks
        # popover can
        # badge finished tasks without per-task follow-up requests.
        task_ids = [m.id for m in metas if m.parent_id]
        statuses = await store.latest_run_statuses(task_ids)
        return [session_info(m, last_run_status=statuses.get(m.id)) for m in metas]

    @router.get("/api/runs", response_model=list[RunInfo])
    async def list_runs() -> list[RunInfo]:
        """Currently-live supervised runs (in-memory; authoritative for active),
        then the runs suspended on a card (``waiting``, from their records)."""
        live = [
            RunInfo(
                session_id=sid,
                run_id=c.record_id,
                agent=deps.name_of(c.agent),  # registry key, same as AgentInfo
                status=c.status,
                turns=c.turns,
                source=c.source,
                started_at=c.started_at,
                pending_approval=dict(approval_data(c.pending_approval))
                if c.pending_approval is not None
                else None,
                waits_on=_live_waits_on(sid, c),
            )
            for sid, c in deps.supervisor
        ]
        busy = {r.session_id for r in live}
        waiting = [
            RunInfo(
                session_id=row.session_id,
                run_id=row.id,
                agent=row.agent or "",
                status="waiting",
                turns=0,
                source=row.source,
                started_at=row.started_at,
                # An approval card only: a question or a tool's own wait has
                # no card a page could answer outside its chat.
                pending_approval=cast(
                    "dict[str, Any] | None",
                    (row.waiting_for or {}).get("request") or None,
                )
                if (row.waiting_for or {}).get("kind", "approval") == "approval"
                else None,
                waits_on=_waits_on(row.waiting_for),
            )
            for row in await store.waiting_runs()
            if row.session_id is not None and row.session_id not in busy
        ]
        return [*live, *waiting]

    def _live_waits_on(sid: str, ctrl: RunController) -> WaitsOn | None:
        if ctrl.pending_approval is not None:
            return WaitsOn(kind="approval", id=ctrl.pending_approval.request.call.id)
        q = deps.questions.pending(sid) if deps.questions is not None else None
        if q is None:
            return None
        # Name the asking call — the same id a suspension of it records, so a
        # page tells the user once. Calls after it in its turn have no result
        # yet either, so match its question; ask_human is an execution
        # barrier, so failing that it is the first call still open.
        open_calls = pending_tool_calls(
            [*ctrl.completed_mirror, *ctrl.current_turn_entries]
        )
        if not open_calls:
            return None
        asking = next(
            (c for c in open_calls if _asks(c.arguments, q.question)), open_calls[0]
        )
        return WaitsOn(kind="question", id=asking.id)

    @router.get("/api/runs/history", response_model=list[RunRecordInfo])
    async def run_history(
        session_id: str | None = Query(None),
        source: str | None = Query(None, max_length=200),
        since: float | None = Query(None),
        run_id: str | None = Query(None, max_length=200),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> list[RunRecordInfo]:
        """Persisted run records, newest first (live runs stay on ``/api/runs``).

        ``since`` keeps only runs finished after that timestamp — how a client
        asks "what completed while I was away?" after a reload. ``run_id``
        looks one run up by the id its lifecycle events carry.
        """
        rows = await store.list_runs(
            session_id=session_id,
            source=source,
            since=since,
            run_id=run_id,
            limit=limit,
            offset=offset,
        )
        return [run_record(r) for r in rows]

    @router.delete("/api/sessions")
    async def delete_all_sessions() -> dict[str, bool]:
        """Delete every session's transcript and metadata (stopping live runs)."""
        # As in delete_session, the mark keeps new runs and tasks out meanwhile.
        async with deps._deleting_every_chat():
            for sid, _ctrl in deps.supervisor:
                deps.supervisor.cancel(sid, discard=True, reason="the chat was deleted")
            # As in delete_session: let every stopped run wind down first —
            # those stopped just now and those stopped earlier but still
            # winding down.
            await deps.supervisor.drain()
            deps.supervisor.last_client.clear()
            deps.supervisor.forget_all()  # runs suspended there end too
            # Reap every chat's workspace session (and its background processes).
            await deps.workspaces.aclose()
            await store.delete_all()
        return {"ok": True}

    @router.get("/api/sessions/{session_id}", response_model=SessionDetail)
    async def get_session(session_id: str) -> SessionDetail:
        meta = await store.get(session_id)
        now = time.time()
        created = meta.created_at if meta else now
        updated = meta.updated_at if meta else now
        entries, active_run_id, waiting = await _session_view(
            deps, session_id, created_at=created, updated_at=updated
        )
        origin = None
        kind = origin_kind(meta.origin_kind) if meta is not None else None
        if meta is not None and kind == "schedule" and meta.origin_ref:
            sched = await store.get_schedule(meta.origin_ref)
            origin = SessionOrigin(
                kind=kind,
                schedule_id=meta.origin_ref,
                title=(sched.name or provisional_title(sched.input)) if sched else None,
                exists=sched is not None,
            )
        elif meta is not None and kind is not None and meta.origin_session_id:
            source = await store.get(meta.origin_session_id)
            origin = SessionOrigin(
                kind=kind,
                session_id=meta.origin_session_id,
                ref=meta.origin_ref,
                title=source.title if source else None,
                exists=source is not None,
            )
        return SessionDetail(
            id=meta.id if meta else session_id,
            title=meta.title if meta else None,
            agent=meta.agent if meta else None,
            created_at=created,
            updated_at=updated,
            entries=entries,
            active_run_id=active_run_id,
            waiting=waiting,
            pinned=meta.pinned if meta else False,
            archived=meta.archived if meta else False,
            parent_id=meta.parent_id if meta else None,
            origin=origin,
            workspace_mode=meta.workspace_mode if meta else None,
            workspace_mode_rev=meta.workspace_mode_rev if meta else 0,
            workspace_root=meta.workspace_root if meta else None,
            model=meta.model if meta else None,
            exists=meta is not None,
        )

    @router.get("/api/sessions/{session_id}/ui-data/{ref}")
    async def get_ui_data(session_id: str, ref: str) -> Response:
        """A UI component's full props, kept out of the model's context by
        ``put_ui_data`` during a run of this chat. A ref never changes."""
        data = await store.get_ui_data(session_id, ref)
        if data is None:
            raise WebError(
                404,
                "ui_data_not_found",
                "no such component data in this chat",
                hint="the chat or its data was deleted; the page shows the preview",
            )
        return Response(
            data,
            media_type="application/json",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    @router.get("/api/sessions/{session_id}/tool-images/{call_id}/{index}")
    async def get_tool_image(session_id: str, call_id: str, index: int) -> Response:
        """Serve one image part of a tool result, straight from the transcript.

        The source of truth is what the model actually saw — not the current
        workspace file (which may have changed or live outside the root), so
        the bytes come from the stored entry. Lookup order mirrors freshness:
        the live run's in-memory mirror first (a checkpoint may not have
        landed yet), then the persisted session, then a resumable
        checkpoint's entries (restart recovery). ``index`` is 0-based over
        the result's *image* parts, matching the stubs in SSE and
        ``MessageOut.images``.
        """

        def find(entries: list[Any]) -> ImagePart | None:
            for entry in reversed(entries):
                if (
                    isinstance(entry, ToolResultEntry)
                    and entry.call_id == call_id
                    and entry.parts
                ):
                    images = [p for p in entry.parts if isinstance(p, ImagePart)]
                    if 0 <= index < len(images):
                        return images[index]
                    # Keep scanning: providers have been seen reusing call
                    # ids, so an older entry under this id may carry the
                    # requested index.
            return None

        image: ImagePart | None = None
        live = deps.supervisor.get(session_id)
        if live is not None:
            image = find([*live.completed_mirror, *live.current_turn_entries])
        if image is None:
            image = find(await session.load(session_id))
        if image is None and store.checkpointer is not None:
            candidate = await store.get_active_run_id(session_id)
            if candidate:
                snapshot = await store.checkpointer.load(candidate)
                if snapshot is not None:
                    image = find(list(snapshot.entries))
        if image is None:
            raise WebError(404, "image_not_found", "no such tool image")
        if image.data is None:
            # A URL-sourced part has no bytes to serve; the client falls back
            # to its chip. 404 keeps the route contract simple.
            raise WebError(404, "image_not_found", "image part is a URL reference")
        try:
            payload = base64.b64decode(image.data, validate=True)
        except binascii.Error:
            raise WebError(
                404, "image_not_found", "image part is not valid base64"
            ) from None
        return Response(
            content=payload,
            media_type=image.mime_type or "application/octet-stream",
            headers={
                # The transcript is append-only: a (call_id, index) never
                # changes its bytes, so the browser may cache hard.
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )

    @router.patch("/api/sessions/{session_id}", response_model=ChatSessionInfo)
    async def update_session(session_id: str, req: SessionPatch) -> ChatSessionInfo:
        """Rename, (un)pin, and/or switch the permission mode or the model
        of a session — applies whichever fields are set."""
        meta = await store.get(session_id)
        if meta is None:
            raise WebError(404, "session_not_found", "session not found")
        mode = req.workspace_mode
        if mode is not None:
            switch = deps.workspace_switch
            if switch is None:
                raise WebError(
                    404,
                    "feature_unavailable",
                    "this server doesn't let chats pick a permission mode",
                )
            if mode not in switch.modes:
                raise WebError(
                    400,
                    "invalid_request",
                    f"mode {mode!r} is not available on this server",
                    hint=f"choose one of: {', '.join(switch.modes)}",
                )
        # An explicit null puts the chat back on the server's default.
        pick_model = "model" in req.model_fields_set
        if pick_model and req.model is not None:
            deps.check_chat_model(req.model)
        root = req.workspace_root
        if root is not None:
            if deps.workspace_switch is None:
                raise WebError(
                    404,
                    "feature_unavailable",
                    "this server doesn't let chats pick a directory",
                )
            if meta.workspace_root is not None:
                raise WebError(
                    409,
                    "workspace_fixed",
                    "this chat already works in a directory",
                    hint="a chat's directory never changes: start a new chat "
                    "in the other one",
                )
            # Bound (and announced) first: a refused directory changes nothing.
            await deps.associate([session_id], root)
        if req.title is not None:
            await store.set_title(session_id, req.title)
        # Archived before pinned: sent together, the pin is what stays.
        if req.archived is not None:
            await store.set_archived(session_id, req.archived)
        if req.pinned is not None:
            await store.set_pinned(session_id, req.pinned)
        if mode is not None:
            # Live: a reply in flight goes on in the new mode, and so do the
            # chat's tasks.
            await deps.switch_mode(session_id, mode)
        if pick_model:
            await store.set_model(session_id, req.model)
        meta = await store.get(session_id)
        if meta is None:  # deleted concurrently between the update and re-read
            raise WebError(404, "session_not_found", "session not found")
        if req.title is not None:
            deps.emit(
                "session_retitled",
                SessionRetitledData(session_id=session_id, title=meta.title),
            )
        if mode is not None:
            deps.emit_workspace(meta)
        return session_info(meta)

    @router.get("/api/sessions/{session_id}/grants", response_model=list[GrantInfo])
    async def list_grants(session_id: str) -> list[GrantInfo]:
        """The chat's remembered approvals, flagged ``active`` when they apply
        to the chat as served now (same agent and workspace root). A task
        lists its chat's: those are the ones its runs use."""
        owner, owner_agent = await deps.grant_owner(session_id)
        rows = await store.list_grants(owner)
        if not rows:
            return []
        current = await _current_grant_context(
            session_id, owner_agent if owner != session_id else None
        )
        return [
            GrantInfo(
                scope=r.scope,
                context=r.context,
                created_at=r.created_at,
                active=r.context == current,
            )
            for r in rows
        ]

    @router.delete("/api/sessions/{session_id}/grants")
    async def revoke_grant(
        session_id: str, scope: str = Query(...), context: str = Query(...)
    ) -> dict[str, bool]:
        """Forget one remembered approval. Later calls in that scope ask
        again; a call it already approved is not affected. A task's are its
        chat's, so this forgets it there."""
        owner, _ = await deps.grant_owner(session_id)
        removed = await store.delete_grant(owner, scope, context)
        return {"removed": removed}

    async def _current_grant_context(
        session_id: str, owner_agent: str | None
    ) -> str | None:
        ctrl = deps.supervisor.get(session_id)
        if ctrl is not None:
            return ctrl.grant_context()
        meta = await store.get(session_id)
        name = owner_agent or (meta.agent if meta else None)
        agent = deps.agents.get(name) if name else None
        if meta is None or name is None or agent is None:
            return None
        # In the chat's own directory (none stored: the agent's own).
        where = ChatBinding(meta.workspace_root, None)
        return agent_grant_context(name, deps.at_root(agent, where))

    @router.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        # Its subagent tasks go with it (``store.delete``), so they stop too.
        # The mark keeps new runs and tasks out of the tree meanwhile (see
        # ``RouterDeps._chat_alive``), so the tree read here is the whole tree.
        async with deps._deleting_tree(session_id) as tree:
            # Stop live runs first (discarding their partial transcripts) —
            # otherwise they keep burning tokens and re-persist entries for the
            # deleted chats when they wind down.
            for sid in tree:
                deps.supervisor.cancel(sid, discard=True, reason="the chat was deleted")
            for sid in tree:
                # Stopping is cooperative: a final model call already in flight
                # still completes, and the run then appends itself to the
                # Session. Wait out the wind-down before deleting the rows (on
                # a timeout, delete anyway).
                await deps.supervisor.drain(sid)
                deps.supervisor.last_client.pop(sid, None)
                deps.supervisor.forget(sid)  # a run suspended there ends too
                # Deleting the chat is what ends its workspace session —
                # background processes deliberately survive run ends, so this
                # is where they die.
                await deps.workspaces.close(sid)
            await store.delete(session_id)
        return {"ok": True}

    # One rewind of a chat at a time: each maps its target onto the transcript
    # as it read it, so an overlapping one would cut by a stale reading.
    rewinding: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @router.post("/api/sessions/{session_id}/rewind", response_model=RewindResponse)
    async def rewind_session(session_id: str, req: RewindRequest) -> RewindResponse:
        """Rewind to just before the user message ``ref`` (or ``user_turn``) names.

        The destructive-undo behind edit-and-resend / regenerate: the target
        user message and everything after it are dropped; the caller then
        sends the (edited or original) text as a fresh turn. Refused while a
        run is live — its in-flight state would resurrect the tail.
        """
        async with rewinding.setdefault(session_id, asyncio.Lock()):
            return await rewind_locked(session_id, req)

    async def rewind_locked(session_id: str, req: RewindRequest) -> RewindResponse:
        if deps.supervisor.get(session_id) is not None:
            raise WebError(
                409,
                "run_active",
                "a run is active for this session",
                hint="stop it first",
            )
        # A run the user just stopped is evicted from the supervisor at once,
        # but its task is still winding down — and that task's `finally` is
        # what folds the interrupted turn into the Session. Rewinding across
        # that window is the edit-and-resend bug: the ordinal is mapped over a
        # transcript still missing its last user turn (404 for a message the
        # user can plainly see), and the late persist would append that turn
        # back after the cut. Cancellation is cooperative, so this waits out
        # whatever tool was mid-flight.
        if not await deps.supervisor.drain(session_id):
            raise WebError(
                409,
                "run_stopping",
                "a run is still stopping for this session",
                hint="try again shortly",
            )
        rewind = getattr(session, "rewind", None)
        if rewind is None:
            raise WebError(
                501,
                "feature_unavailable",
                "the configured session store does not support rewind",
            )
        # A resumable checkpoint's snapshot may hold user turns the client
        # rendered (the spliced view) that the store doesn't — count them so
        # ordinal mapping agrees with what the client saw. Read-only for now:
        # nothing is destroyed until the ordinal validates.
        ckpt_user_turns = 0
        runs = segments_runs(await session.segments(session_id))
        entries = [e for run in runs for e in run.entries]
        stale = await store.get_active_run_id(session_id)
        if stale and store.checkpointer is not None:
            snapshot = await store.checkpointer.load(stale)
            if snapshot is not None and snapshot.status in RESUMABLE:
                ckpt_user_turns = sum(
                    1
                    for e in snapshot.entries
                    if isinstance(e, InputEntry) and e.role == "user"
                )
                runs.append(_in_flight(snapshot))
        user_turn = req.user_turn
        if req.ref is not None:
            refs = user_turn_refs(runs)
            if req.ref not in refs:
                raise WebError(404, "turn_not_found", f"message {req.ref!r} not found")
            user_turn = refs.index(req.ref)
        assert user_turn is not None  # RewindRequest takes exactly one
        # Map the user-turn ordinal onto the flat entry index that starts it.
        cut = None
        seen = -1
        for i, entry in enumerate(entries):
            if isinstance(entry, InputEntry) and entry.role == "user":
                seen += 1
                if seen == user_turn:
                    cut = i
                    break
        if cut is None and user_turn > seen + ckpt_user_turns:
            # Out of range (e.g. a stale client): refuse BEFORE touching the
            # checkpoint — a bad ordinal must not cost a resumable run.
            raise WebError(404, "turn_not_found", f"user turn {user_turn} not found")
        # Validated — now the destructive part. The resume pointer dies with
        # every rewind (its snapshot replays a tail that no longer exists);
        # a cut inside the checkpoint's own turns means "drop the checkpoint,
        # keep the whole stored transcript" (removed = 0).
        if stale:
            if store.checkpointer is not None:
                await store.checkpointer.delete(stale)
            await store.clear_active_run_id(session_id, expected=stale)
        removed = 0 if cut is None else await rewind(session_id, keep_entries=cut)
        meta = await store.get(session_id)
        now = time.time()
        view, _, _ = await _session_view(
            deps,
            session_id,
            created_at=meta.created_at if meta else now,
            updated_at=meta.updated_at if meta else now,
        )
        return RewindResponse(removed=removed, entries=view)

    @router.post("/api/sessions/{session_id}/fork", response_model=ForkResponse)
    async def fork_session(session_id: str, req: ForkRequest) -> ForkResponse:
        """Copy the chat — up to just before the user message ``ref`` names,
        or whole — into a new, independent chat.

        A fork copies finished history only (see ``ChatStore.fork``), so it
        never touches a run or a checkpoint. A ``ref`` names a message of a
        finished run, so a run going on meanwhile changes nothing. A whole copy
        means "the chat as I see it", which a live, stopping or resumable run
        extends past the finished history: refused until that run settles.
        """
        async with deps._chat_alive(session_id) as alive:
            pass
        if not alive:
            raise WebError(404, "session_not_found", f"unknown session {session_id}")
        if req.ref is None:
            if deps.supervisor.get(session_id) is not None:
                raise WebError(
                    409,
                    "run_active",
                    "a run is active for this session",
                    hint="duplicate the chat once it finishes",
                )
            if not await deps.supervisor.drain(session_id):
                raise WebError(
                    409,
                    "run_stopping",
                    "a run is still stopping for this session",
                    hint="try again shortly",
                )
            stale = await store.get_active_run_id(session_id)
            if stale and store.checkpointer is not None:
                snapshot = await store.checkpointer.load(stale)
                if snapshot is not None and snapshot.status in RESUMABLE:
                    raise WebError(
                        409,
                        "run_unfinished",
                        "the chat's last run was interrupted",
                        hint="resume it or send a new message first",
                    )
        try:
            fork_id = await store.fork(session_id, ref=req.ref)
        except ValueError:
            raise WebError(
                404, "turn_not_found", f"message {req.ref!r} not found"
            ) from None
        except LookupError:
            raise WebError(
                404, "session_not_found", f"unknown session {session_id}"
            ) from None
        if fork_id is None:
            raise WebError(
                409,
                "nothing_to_fork",
                "there is no finished history to copy",
                hint="fork from a later message",
            )
        meta = await store.get(fork_id)
        deps.emit(
            "session_created",
            SessionCreatedData(
                session_id=fork_id,
                agent=meta.agent if meta else None,
                title=meta.title if meta else None,
            ),
        )
        return ForkResponse(session_id=fork_id)

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

    @router.post(
        "/api/sessions/{session_id}/followups", response_model=FollowupsResponse
    )
    async def suggest_followups(session_id: str) -> FollowupsResponse:
        """Questions the user might ask next, for the UI's follow-up chips.

        POST rather than GET like the sibling read-only routes: this spends
        model tokens, so it must not look safe to prefetch or auto-retry.
        Empty whenever the feature is off, no turn has been answered yet, or
        the suggester declined — the UI then renders nothing.
        """
        meta = await store.get(session_id)
        if meta is None:
            raise WebError(404, "session_not_found", f"unknown session {session_id}")
        request = FollowupRequest(
            session_id=session_id,
            agent=deps.agent_of(meta) or "",
            messages=entries_to_messages(await session.load(session_id)),
        )
        return FollowupsResponse(followups=await deps.suggest_followups(request))

    @router.get("/api/sessions/{session_id}/export")
    async def export_session(
        session_id: str, format: str = Query("md", pattern="^(md|json|txt)$")
    ) -> Any:
        """Export a session as markdown, JSON, or plain text."""
        meta = await store.get(session_id)
        entries = await session.load(session_id)
        msgs = entries_to_messages(entries)

        if format == "json":
            # One per tool message, in order (see tool_result_flags).
            flags = iter(tool_result_flags(entries))
            return JSONResponse(
                content={
                    "session_id": session_id,
                    "title": meta.title if meta else None,
                    "agent": meta.agent if meta else None,
                    "messages": [
                        message_to_json_dict(
                            m, flags=next(flags, None) if m.role == "tool" else None
                        )
                        for m in msgs
                    ],
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
