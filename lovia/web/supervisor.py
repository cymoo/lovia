"""Server-owned run supervision: runs that outlive the HTTP connection.

A streaming agent run becomes a long-lived ``asyncio.Task`` owned by the
process, not the request. SSE endpoints subscribe to the run's :class:`EventHub`
and may **detach** (disconnect) and **re-attach** without affecting the run.
There is at most one run per session; the supervisor consolidates the
per-session cancel token + mailbox that the request handler used to own.

Single-worker by design (like the rest of the web layer's per-process state):
the hub + task live in one process. The hub is intentionally a small, swappable
class — a Redis-backed implementation could replace it without touching the
loop or the endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

try:
    from fastapi import HTTPException
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from .. import events
from ..checkpointer import CheckpointOptions
from ..messages import Message, Usage
from ..reliability import CancelToken
from ..runner import Runner
from ..runtime.checkpoint import CheckpointWriter
from ..steering import Mailbox
from ..transcript import (
    InputEntry,
    ToolResultEntry,
    TranscriptEntry,
    drop_dangling_tool_calls,
)
from .api.serialization import drop_system_entries, view_messages
from .schemas import MessageOut
from .sse import event_to_sse, usage_dict
from .store import RunRow

if TYPE_CHECKING:
    from ..agent import Agent
    from ..checkpointer import RunSnapshot
    from .api.deps import RouterDeps

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# EventHub — synchronous fan-out + monotonic seq
# --------------------------------------------------------------------------- #


class _Overflow(Exception):
    """A subscriber fell too far behind and was dropped; it must re-attach."""


_CLOSED = object()  # run terminal → close the SSE normally
_DROPPED = object()  # subscriber overflowed → close the SSE so the client re-attaches


class _Subscription:
    __slots__ = ("_hub", "_q")

    def __init__(self, hub: EventHub, maxsize: int) -> None:
        self._hub = hub
        self._q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)

    def __aiter__(self) -> _Subscription:
        return self

    async def __anext__(self) -> tuple[int, Any]:
        item = await self._q.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        if item is _DROPPED:
            raise _Overflow()
        return cast("tuple[int, Any]", item)

    def close(self) -> None:
        self._hub._unsubscribe(self)


class EventHub:
    """Fan-out of events to per-subscriber queues, with a monotonic ``seq``.

    ``publish`` is synchronous (no ``await``), so it cannot interleave with a
    snapshot capture — the basis of the attach no-gap/no-overlap guarantee.

    The payload is opaque: each ``RunController`` runs one hub of
    :class:`~lovia.events.Event`, and ``RouterDeps.bus`` runs one process-wide
    hub of pre-encoded SSE dicts (the ``/api/events`` lifecycle stream).
    """

    def __init__(self, *, queue_maxsize: int = 512) -> None:
        self._subs: set[_Subscription] = set()
        self._seq = 0
        self._maxsize = queue_maxsize
        self._closed = False

    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, ev: Any) -> int:
        self._seq += 1
        for sub in list(self._subs):
            try:
                sub._q.put_nowait((self._seq, ev))
            except asyncio.QueueFull:
                self._drop(sub)
        return self._seq

    def subscribe(self) -> _Subscription:
        sub = _Subscription(self, self._maxsize)
        if self._closed:
            sub._q.put_nowait(_CLOSED)
        else:
            self._subs.add(sub)
        return sub

    def close(self) -> None:
        self._closed = True
        for sub in list(self._subs):
            # Deliver any still-queued events, then the close sentinel — do NOT
            # drain, or a fast terminal publish + close loses the tail (e.g. the
            # final `done`/`error`). Only a full queue (overflow) is drained.
            try:
                sub._q.put_nowait(_CLOSED)
            except asyncio.QueueFull:
                self._drain(sub)
                sub._q.put_nowait(_CLOSED)
        self._subs.clear()

    def _drop(self, sub: _Subscription) -> None:
        self._subs.discard(sub)
        self._drain(sub)
        sub._q.put_nowait(_DROPPED)

    def _unsubscribe(self, sub: _Subscription) -> None:
        self._subs.discard(sub)

    @staticmethod
    def _drain(sub: _Subscription) -> None:
        while True:
            try:
                sub._q.get_nowait()
            except asyncio.QueueEmpty:
                break


# --------------------------------------------------------------------------- #
# RunController — one supervised run per session
# --------------------------------------------------------------------------- #


@dataclass
class _Attachment:
    """What a re-attaching subscriber receives: an authoritative snapshot of the
    completed turns, the current turn's events to replay, and the live tail."""

    snapshot: list[MessageOut]
    buffered: list[events.Event]
    subscription: _Subscription
    status: str


class RunController:
    """Owns a run's supervised task, cancel token, mailbox, event hub, and the
    snapshot mirror used to serve late subscribers.

    The snapshot state (``history_baseline``/``completed_mirror``/
    ``in_flight_buffer``/``pending_approval``) is mutated **only** inside the
    task, synchronously, immediately before each ``hub.publish`` — so
    :meth:`attach` can read a consistent snapshot against the hub's ``seq``.
    """

    def __init__(
        self,
        *,
        deps: RouterDeps,
        supervisor: RunSupervisor,
        session_id: str,
        agent: Agent[Any],
        first_input: str | list[Message],
        first_checkpoint: CheckpointOptions | None,
        seed_entries: list[TranscriptEntry],
        is_new: bool,
        title_message: str | None,
        source: str,
    ) -> None:
        self.deps = deps
        self.supervisor = supervisor
        self.session_id = session_id
        self.agent = agent
        self.source = source
        self.cancel = CancelToken()
        self.mailbox = Mailbox()
        self.hub = EventHub()
        self.run_id = (
            first_checkpoint.resolved_run_id if first_checkpoint is not None else None
        )
        self.status = "running"
        self.turns = 0
        self.succeeded = False
        self.final_output: Any = None
        self.started_at = time.time()
        # Cumulative usage across auto-chained legs, for the durable run record.
        self.usage = Usage()
        # Final model call's prompt size (context fill), from the last leg that
        # reported one — rides in the run record next to the cumulative usage.
        self.last_input_tokens: int | None = None
        # snapshot mirror (task-private until read synchronously by attach)
        self.history_baseline: list[TranscriptEntry] = []
        self.completed_mirror: list[TranscriptEntry] = []
        self.current_turn_entries: list[TranscriptEntry] = []
        self.in_flight_buffer: list[events.Event] = []
        self.pending_approval: events.ApprovalRequired | None = None
        # first-run spec
        self._first_input = first_input
        self._first_ckpt = first_checkpoint
        self._seed_entries = seed_entries
        self._is_new = is_new
        self._title_message = title_message
        self._user_cancelled = False
        # Set when the session itself is being deleted: skip the partial-persist
        # in the finally, or the wind-down would re-create transcript rows for a
        # chat that no longer exists.
        self._discard_partial = False
        self.task: asyncio.Task[None] | None = None

    # -- lifecycle ------------------------------------------------------- #

    def _ensure_begun(self) -> None:
        """Create the supervised task on the first subscribe, so the very first
        events (RunStarted/early deltas) can never be published before a
        subscriber exists."""
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    def subscribe_live(self) -> _Subscription:
        """Subscribe to a fresh run (START/resume) and begin it. No snapshot."""
        sub = self.hub.subscribe()
        self._ensure_begun()
        return sub

    def attach(self, *, with_snapshot: bool) -> _Attachment:
        """Re-attach to a live run: authoritative snapshot + current-turn replay
        + live tail, captured atomically against the hub seq."""
        sub = self.hub.subscribe()
        buffered = list(self.in_flight_buffer)
        if with_snapshot:
            entries = [*self.history_baseline, *self.completed_mirror]
            now = time.time()
            snapshot = view_messages(entries, created_at=now, updated_at=now)
        else:
            snapshot = []
        # Belt-and-suspenders: a parked approval is in the buffer, but re-add it
        # if it somehow isn't, so a late client can always decide.
        if self.pending_approval is not None and self.pending_approval not in buffered:
            buffered = [*buffered, self.pending_approval]
        return _Attachment(
            snapshot=snapshot, buffered=buffered, subscription=sub, status=self.status
        )

    def inject(self, message: str) -> int:
        return self.mailbox.push(message)

    def uninject(self, token: int) -> bool:
        return self.mailbox.remove(token)

    def cancel_run(self, *, user: bool) -> None:
        self._user_cancelled = self._user_cancelled or user
        self.cancel.cancel("user requested stop" if user else "server shutdown")
        # Unblock a run parked on a pending approval (deny) so it can wind down.
        self.deps.approvals.deny_pending(self.session_id)

    async def _await_approval(self, ev: events.ApprovalRequired) -> None:
        """Await the HTTP decision for ``ev``, denying after ``approval_timeout``.

        Without the timeout a clientless (scheduled) run parked on an approval
        holds its concurrency slot forever — ``RunBudget.max_seconds`` is only
        checked at turn boundaries, which a parked run never reaches.
        """
        decision = self.deps.approvals.await_decision(self.session_id, ev)
        if self.deps.approval_timeout is None:
            await decision
            return
        try:
            await asyncio.wait_for(decision, self.deps.approval_timeout)
        except asyncio.TimeoutError:
            # wait_for cancelled the awaiter, which default-denies on the way
            # out — the runner unblocks with a rejection; the run continues.
            log.info(
                "approval %s for session %s timed out after %.0fs: denied",
                ev.call.id,
                self.session_id,
                self.deps.approval_timeout,
            )

    async def _persist_partial(self, run_id: str) -> None:
        """Fold this run's completed turns into the durable Session.

        Called when a run ends without success and its checkpoint is about to be
        dropped (user stop, or a non-resumable failure) so a reload shows what was
        produced instead of an empty chat. ``completed_mirror`` grows only on
        ``TurnEnded``, so a *fresh* run's mirror already ends on a whole turn — but
        a *resumed* run seeds it with the checkpoint's entries, which can end on a
        tool call the restored run had not yet executed. ``drop_dangling_tool_calls``
        strips any such unmatched call so the stored transcript never ends on a
        dangling ``tool_use`` (which a provider would reject on the next turn).

        Keyed by ``run_id`` -> idempotent; with the checkpoint deleted next, the
        run lives in exactly one place (the resume path concatenates session
        history + snapshot, so a run present in both would double-count).
        """
        session = self.deps.session
        entries = drop_dangling_tool_calls(list(self.completed_mirror))
        if session is not None and entries:
            await session.append(self.session_id, entries, run_id=run_id)

    # -- snapshot mirror ------------------------------------------------- #

    def _ingest(self, ev: events.Event) -> None:
        if isinstance(ev, events.TurnStarted):
            self.turns = ev.turn
            self.in_flight_buffer = []
            self.current_turn_entries = []
        if isinstance(
            ev,
            (
                events.TurnStarted,
                events.TextDelta,
                events.ReasoningDelta,
                events.OutputDiscarded,
                events.MessageCompleted,
                events.ToolCallStarted,
                events.ToolCallCompleted,
                events.ApprovalRequired,
                events.UserMessageInjected,
                events.HandoffOccurred,
                events.ContextCompacted,
                events.ToolCallFailed,
                events.RunCompleted,
                events.RunFailed,
            ),
        ):
            self.in_flight_buffer.append(ev)
        if isinstance(ev, events.UserMessageInjected):
            self.current_turn_entries.append(
                InputEntry(role="user", content=ev.content)
            )
        elif isinstance(ev, events.MessageCompleted):
            self.current_turn_entries.extend(ev.entries)
        elif isinstance(ev, events.ToolCallCompleted):
            self.current_turn_entries.append(
                ToolResultEntry(
                    call_id=ev.call.id,
                    output=ev.output,
                    is_error=ev.is_error,
                )
            )
        elif isinstance(ev, events.TurnEnded):
            self.completed_mirror.extend(self.current_turn_entries)
            self.current_turn_entries = []
            self.in_flight_buffer = []
        if isinstance(ev, events.OutputDiscarded):
            # A retry wiped this turn's partial output — drop the buffered deltas.
            self.in_flight_buffer = [
                e
                for e in self.in_flight_buffer
                if not isinstance(e, (events.TextDelta, events.ReasoningDelta))
            ]

    def _publish(self, ev: events.Event) -> None:
        self._ingest(ev)
        self.hub.publish(ev)

    # -- the supervised task --------------------------------------------- #

    async def _run(self) -> None:
        deps, sid = self.deps, self.session_id
        session, store = deps.session, deps.store
        next_input: str | list[Message] = self._first_input
        ckpt = self._first_ckpt
        seed = self._seed_entries
        title_args: tuple[str, str, Any, str] | None = None
        succeeded = False
        error_seen = False
        final_error: str | None = None  # message for the durable run record
        failed_terminally = False  # ended in a non-resumable failure (drop checkpoint)
        # The durable run record. Keyed by the first leg's run_id so a resumed
        # run finalizes the row it opened; minted when checkpointing is off.
        # Best-effort bookkeeping: never let it break the run itself.
        record_id = self.run_id or uuid.uuid4().hex
        try:
            await store.start_run(
                RunRow(
                    id=record_id,
                    session_id=sid,
                    agent=deps.name_of(self.agent),
                    source=self.source,
                    status="running",
                    error=None,
                    started_at=self.started_at,
                    finished_at=None,
                    usage=None,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("run record insert failed for %s: %s", sid, exc)
        deps.emit(
            "run_started",
            session_id=sid,
            run_id=record_id,
            agent=deps.name_of(self.agent),
            source=self.source,
        )
        try:
            while True:
                self.run_id = ckpt.resolved_run_id if ckpt is not None else None
                # Bail before starting a leg that was cancelled mid-setup. On an
                # auto-chain hop the cancel endpoint can capture the prior leg's
                # run_id (this leg's pointer advanced a step earlier in
                # _checkpoint_for), so its eager active_run_id clear no-ops.
                # Starting this leg would then persist an ``interrupted``
                # checkpoint the stale clear left reachable, and a reconnect could
                # revive the stopped run; not starting it lets the finally clear
                # the pointer cleanly. self.run_id is set above, so cleanup targets
                # this leg.
                if self.cancel.is_cancelled:
                    break
                self.history_baseline = (
                    await session.load(sid) if session is not None else []
                )
                self.completed_mirror = list(seed)
                self.current_turn_entries = []
                self.in_flight_buffer = []
                budget = deps.fresh_budget()
                handle = Runner.stream(
                    self.agent,
                    next_input,
                    session=session,
                    session_id=sid,
                    context_policy=deps.context_policy,
                    cancel_token=self.cancel,
                    mailbox=self.mailbox,
                    max_turns=deps.max_turns,
                    budget=budget,
                    retry=deps.retry,
                    tracer=deps.tracer,
                    checkpoint=ckpt,
                )
                succeeded = False
                error_seen = False
                async for ev in handle:
                    if isinstance(ev, events.RunFailed):
                        error_seen = True
                    self._publish(ev)
                    if isinstance(ev, events.RunCompleted):
                        succeeded = True
                        self.succeeded = True
                        self.final_output = ev.result.output
                        self.usage.add(ev.result.usage)
                        if ev.result.last_input_tokens is not None:
                            self.last_input_tokens = ev.result.last_input_tokens
                    if isinstance(ev, events.ApprovalRequired):
                        # The loop gates on the consumer: do not advance the
                        # iterator until the decision lands (mirrors the old
                        # drive_stream). The awaiter is THIS task, not the HTTP
                        # request, so a detach can't release it.
                        self.pending_approval = ev
                        self.status = "blocked_on_approval"
                        try:
                            await self._await_approval(ev)
                        finally:
                            self.pending_approval = None
                            self.status = "running"
                if not succeeded:
                    # The stream ends with RunFailed instead of raising;
                    # re-raise the terminal error here so the except path
                    # below classifies and persists it exactly as before.
                    await handle.result()
                if (
                    self._is_new
                    and succeeded
                    and title_args is None
                    and self._title_message is not None
                ):
                    title_args = (
                        sid,
                        self._title_message,
                        self.final_output,
                        # Registry key, not agent.name: schedule_title re-picks
                        # the agent from the registry by this string.
                        deps.name_of(self.agent),
                    )
                # Auto-chain: leftovers re-queued, next run starts with empty
                # input so the loop drains+emits them as user turns (Phase 1).
                leftover = self.mailbox.drain()
                if not leftover or self.cancel.is_cancelled or not succeeded:
                    break
                for content in leftover:
                    self.mailbox.push(content)
                next_input = []
                seed = []
                ckpt = await self.supervisor._checkpoint_for(sid, uuid.uuid4().hex)
            if succeeded and store.checkpointer is not None:
                await store.clear_active_run_id(sid, expected=self.run_id)
        except Exception as exc:
            # A terminal failure (MaxTurnsExceeded, provider error, …) that the
            # loop didn't already surface as an `error` event: synthesize one so
            # subscribers see a clear notice, mirroring the old drive_stream.
            log.warning("supervised run %s ended: %s", sid, exc)
            final_error = str(exc) or exc.__class__.__name__
            if not error_seen:
                # Publish via _publish so the synthesized error also lands in the
                # snapshot mirror / in-flight buffer — a dropped client that
                # re-attaches then still replays the terminal error.
                self._publish(events.RunFailed(error=exc))
            # A non-resumable ("failed") end is never offered for reconnect, so the
            # next GET would silently drop its checkpoint and the partial work would
            # vanish on reload. Flag it so the finally folds it into the Session.
            failed_terminally = CheckpointWriter.classify(exc) == "failed"
        finally:
            # Terminal disposition of THIS leg's run_id (target our own run_id, not
            # a re-read pointer — a newer run may have claimed it; the expected=
            # guards below likewise won't clobber that run):
            #   * user stop, or a non-resumable failure -> persist the partial
            #     transcript, then drop the checkpoint + pointer so exactly one
            #     durable copy survives a reload (no resume, hence no double-count).
            #   * transient interrupt / graceful shutdown -> keep the checkpoint so
            #     a reconnect can resume it; the Session is left untouched.
            #   * success -> the loop already persisted + cleared; nothing to do.
            rid = self.run_id
            # A leg that didn't complete published no RunCompleted, so its
            # spend lives only in the checkpoint head — fold it in before the
            # snapshot is (possibly) deleted below. Best effort, like the rest
            # of the run record.
            if not succeeded and rid and store.checkpointer is not None:
                try:
                    snapshot = await store.checkpointer.load(rid)
                    if snapshot is not None:
                        self.usage.add(snapshot.usage)
                        if snapshot.last_input_tokens is not None:
                            self.last_input_tokens = snapshot.last_input_tokens
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("usage read failed for run %s: %s", rid, exc)
            if (
                store.checkpointer is not None
                and rid
                and (self._user_cancelled or failed_terminally)
            ):
                # ``not succeeded`` skips a leg the loop already persisted (e.g. a
                # cancel landing on an auto-chain hop, where this run_id names the
                # not-yet-started next leg and the mirror is the prior, saved one).
                # ``_discard_partial`` skips it when the session is being deleted.
                if not succeeded and not self._discard_partial:
                    await self._persist_partial(rid)
                await store.checkpointer.delete(rid)
                await store.clear_active_run_id(sid, expected=rid)
            await deps.approvals.release(sid)
            self.hub.close()
            self.supervisor._evict(sid, self)
            if title_args is not None:
                deps.schedule_title(*title_args)
            if not succeeded and (self._user_cancelled or final_error is None):
                # Stable outcome text for every cooperative stop: a user cancel
                # surfaces as RunCancelled ("user requested stop"), a shutdown
                # reaches here with no exception at all — both must read
                # "cancelled", not whatever the exception said.
                final_error = "cancelled"
            if succeeded:
                status = "completed"
            elif self._user_cancelled:
                status = "cancelled"
            elif failed_terminally:
                status = "failed"
            else:
                # Cooperative shutdown or a resumable failure: the checkpoint
                # survives, so a reconnect may pick this run back up — in which
                # case start_run flips this same row back to "running".
                status = "interrupted"
            try:
                await store.finish_run(
                    record_id,
                    status=status,
                    error=None if succeeded else final_error,
                    usage=usage_dict(
                        self.usage, last_input_tokens=self.last_input_tokens
                    )
                    if self.usage.total_tokens
                    else None,
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("run record write failed for %s: %s", sid, exc)
            deps.emit(
                "run_finished",
                session_id=sid,
                run_id=record_id,
                status=status,
                error=None if succeeded else final_error,
                source=self.source,
            )


# --------------------------------------------------------------------------- #
# RunSupervisor — process-wide registry (one controller per session)
# --------------------------------------------------------------------------- #


class RunSupervisor:
    def __init__(self, deps: RouterDeps) -> None:
        self.deps = deps
        self._controllers: dict[str, RunController] = {}

    @property
    def max_background_runs(self) -> int:
        return self.deps.max_background_runs

    def get(self, session_id: str) -> RunController | None:
        return self._controllers.get(session_id)

    def __iter__(self) -> Any:
        return iter(list(self._controllers.items()))

    async def _checkpoint_for(self, sid: str, run_id: str) -> CheckpointOptions | None:
        store = self.deps.store
        if store.checkpointer is None:
            return None
        await store.set_active_run_id(sid, run_id)
        return CheckpointOptions(
            checkpointer=store.checkpointer,
            run_id=run_id,
            delete_on_success=True,
        )

    async def start(
        self,
        *,
        session_id: str,
        agent: Agent[Any],
        input: str,
        is_new: bool,
        title_message: str | None,
        autostart: bool = False,
        source: str = "user",
    ) -> RunController:
        if session_id in self._controllers:
            raise HTTPException(
                status_code=409, detail="a run is already active for this session"
            )
        if len(self._controllers) >= self.max_background_runs:
            raise HTTPException(status_code=429, detail="too many concurrent runs")
        seed: list[TranscriptEntry] = (
            [InputEntry(role="user", content=input)] if input else []
        )
        ctrl = RunController(
            deps=self.deps,
            supervisor=self,
            session_id=session_id,
            agent=agent,
            first_input=input,
            first_checkpoint=None,
            seed_entries=seed,
            is_new=is_new,
            title_message=title_message,
            source=source,
        )
        # Reserve the session slot BEFORE the checkpoint await: two concurrent
        # starts (e.g. two tabs submitting at once) would otherwise both pass the
        # caller's live-check and the second would silently orphan the first's
        # running task. The loser now gets the 409 above and attaches instead.
        self._controllers[session_id] = ctrl
        try:
            ckpt = await self._checkpoint_for(session_id, uuid.uuid4().hex)
        except BaseException:
            self._controllers.pop(session_id, None)
            # A concurrent request may have attached during that await; close
            # the hub so its SSE ends instead of waiting on a task that will
            # never start. (After a *successful* start there is no such hang:
            # the caller reaches subscribe_live with no awaits in between, and
            # autostart begins the task right below.)
            ctrl.hub.close()
            raise
        ctrl._first_ckpt = ckpt
        ctrl.run_id = ckpt.resolved_run_id if ckpt is not None else None
        if autostart:
            # Clientless (scheduled) run: begin the task now, with no subscriber.
            ctrl._ensure_begun()
        return ctrl

    async def start_resume(
        self, *, session_id: str, agent: Agent[Any], snapshot: RunSnapshot
    ) -> RunController:
        if session_id in self._controllers:
            raise HTTPException(
                status_code=409, detail="a run is already active for this session"
            )
        ckpt = CheckpointOptions(
            checkpointer=self.deps.store.checkpointer,
            resume_from=snapshot,
            delete_on_success=True,
        )
        seed = drop_system_entries(list(snapshot.entries))
        ctrl = RunController(
            deps=self.deps,
            supervisor=self,
            session_id=session_id,
            agent=agent,
            first_input=[],
            first_checkpoint=ckpt,
            seed_entries=seed,
            is_new=False,
            title_message=None,
            # The resumer's identity; if the run already has a record (it
            # normally does — same run_id), start_run keeps the original source.
            source="user",
        )
        self._controllers[session_id] = ctrl
        return ctrl

    def cancel(self, session_id: str, *, discard: bool = False) -> bool:
        """Cancel the live run, if any. ``discard`` additionally drops the
        partial transcript instead of persisting it — for session deletion."""
        ctrl = self._controllers.get(session_id)
        if ctrl is None:
            return False
        # Evict synchronously so an immediate follow-up /stream starts fresh;
        # the task winds down + deletes its checkpoint in the background.
        self._controllers.pop(session_id, None)
        ctrl._discard_partial = discard
        ctrl.cancel_run(user=True)
        return True

    def _evict(self, session_id: str, ctrl: RunController) -> None:
        if self._controllers.get(session_id) is ctrl:
            self._controllers.pop(session_id, None)

    async def shutdown(self, *, grace: float = 10.0) -> None:
        ctrls = list(self._controllers.values())
        for c in ctrls:
            c.cancel_run(user=False)  # cooperative stop; KEEP the checkpoint
        tasks = [c.task for c in ctrls if c.task is not None]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=grace)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)


# --------------------------------------------------------------------------- #
# SSE bridge
# --------------------------------------------------------------------------- #


async def forward(
    source: _Attachment | _Subscription,
    *,
    sid: str,
    emit_session: bool,
) -> AsyncIterator[dict[str, str]]:
    """Stream a subscription (START/resume) or a re-attach to SSE payloads.

    On client disconnect, sse-starlette cancels this generator → the ``finally``
    unsubscribes (the entire **detach** mechanism); the run is never touched.
    """
    if emit_session:
        yield {"event": "session", "data": json.dumps({"session_id": sid})}
    if isinstance(source, _Attachment):
        sub = source.subscription
        yield {
            "event": "snapshot",
            "data": json.dumps(
                {
                    "session_id": sid,
                    "status": source.status,
                    "entries": [m.model_dump() for m in source.snapshot],
                }
            ),
        }
        for ev in source.buffered:
            payload = event_to_sse(ev)
            if payload is not None:
                yield payload
    else:
        sub = source
    try:
        async for _seq, ev in sub:
            payload = event_to_sse(ev)
            if payload is not None:
                yield payload
    except _Overflow:
        return  # subscriber fell behind → close the SSE so the client re-attaches
    finally:
        sub.close()
