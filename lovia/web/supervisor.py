"""Server-owned run supervision: runs that outlive the HTTP connection.

A chat run — streamed or blocking — becomes a long-lived ``asyncio.Task``
owned by the process, not the request. SSE endpoints subscribe to the run's
:class:`EventHub` and may **detach** (disconnect) and **re-attach** without
affecting the run.
There is at most one run per session; the supervisor consolidates the
per-session cancel token + mailbox that the request handler used to own.

Single-worker by design (like the rest of the web layer's per-process state):
the hub + task live in one process. The hub is intentionally a small, swappable
class — a Redis-backed implementation could replace it without touching the
loop or the endpoints.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import time
import uuid
import weakref
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, cast

from .. import events
from ..approvals import Approval, _Suspension
from ..checkpointer import CheckpointOptions
from ..exceptions import RunSuspended
from ..log_config import CURRENT_AGENT
from ..messages import Message, ToolCall, Usage
from ..reliability import CancelToken, RunBudget
from ..parts import coerce_parts
from ..runner import Runner
from ..runtime.checkpoint import CheckpointWriter
from ..runtime.loop import pending_tool_calls
from ..runtime.resume import normalize_replayed_entries, reachable_agents
from ..session import STATE_META_KEY, TIMING_META_KEY
from ..steering import InjectedContent, Mailbox
from ..transcript import (
    AssistantTextEntry,
    InputEntry,
    ReasoningEntry,
    ToolResultEntry,
    TranscriptEntry,
    entry_to_dict,
    input_to_entries,
)
from ..workspace import ProcessOutput, ProcessStart
from .attachments import attachments_for_run
from .api.serialization import (
    Run,
    Runs,
    drop_system_entries,
    segment_timing,
    segments_runs,
    user_turn_refs,
    view_messages,
)
from .components import UI_DATA_SINK
from .errors import WebError, run_error_code
from .schemas import MessageOut
from .sse import (
    ApprovalAsked,
    ApprovalResolved,
    RunFinishedData,
    RunStartedData,
    RunStatusData,
    SessionData,
    LegFrame,
    LegTiming,
    MCPUnavailable,
    SnapshotData,
    Suspended,
    UserTurnRefs,
    approval_data,
    event_to_sse,
    frame,
    usage_dict,
)
from ..types import JsonObject
from ..workspace.remote import RemoteWorkspace
from ..workspace.workspace import LocalWorkspace, WorkspaceSessionBinding
from .run_deps import RUN_CLIENT, RUN_PROJECT, RUN_SERVER
from .store import RunRow, grant_context
from .waiting import find_waiting
from .workspaces import ChatBinding

if TYPE_CHECKING:
    from ..agent import Agent
    from ..checkpointer import RunSnapshot
    from .api.deps import RouterDeps

log = logging.getLogger(__name__)

# What a stop says when its caller names no one else (the HTTP stop).
USER_STOP = "user requested stop"

# The result a stopped run's unanswered tool call is saved with.
_INTERRUPTED = (
    "Interrupted: the run stopped before this tool returned; its outcome is unknown."
)


@dataclass(frozen=True)
class Kept:
    """What a run suspended in this process resumes with (see
    ``RunSupervisor.kept``): the agent it was handed, its mailbox and the
    source that started it."""

    agent: Agent[Any]
    mailbox: Mailbox
    source: str
    is_task: bool


def _described(arguments: str | None) -> str:
    """The ``description`` a tool call gave (shell's: what the command does)."""
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return ""
    title = args.get("description") if isinstance(args, dict) else None
    # A title, not a page: the record keeps it on its first line.
    return " ".join(title.split())[:200] if isinstance(title, str) else ""


def _queued_entry(content: InjectedContent) -> JsonObject:
    """A message a suspended run has not read yet, as its record keeps it."""
    return entry_to_dict(InputEntry(role="user", content=content))


def agent_grant_context(name: str, agent: "Agent[Any]") -> str:
    """The :func:`~lovia.web.store.grant_context` of an agent as served: its
    name and where its workspace is — a local root, or a remote host and the
    root on it (neither without one of those two)."""
    ws = agent.workspace
    if isinstance(ws, WorkspaceSessionBinding):
        ws = ws.workspace
    if isinstance(ws, RemoteWorkspace):
        return grant_context(name, ws.root, host=ws.host.name)
    root = ws.root if isinstance(ws, LocalWorkspace) else None
    return grant_context(name, root)


# What started the run this task serves: "user" | "schedule:<id>" |
# "subagent:<session>" (a child task run) | "subagent-report:<task>" (the
# clientless run that delivers a report) | "spawn:<session>" (a chat that
# session's agent started). Set at the top of every supervised
# task, so every log line emitted inside that task's context — core RunLoop
# lines included, since ``create_task`` copies the context — can be
# attributed to its run.
RUN_SOURCE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lovia_web_run_source", default=None
)


class _RunSourceFilter(logging.Filter):
    """Stamps ``record.run_source`` (``"[<source>/<agent>] "`` or ``""``).

    *source* is what started the supervised run (``RUN_SOURCE``); *agent* is
    who is running right now (:data:`lovia.log_config.CURRENT_AGENT`, kept
    current by the core loop). Either may be absent.

    Attach to a *handler* (filters on loggers don't apply to child loggers)
    and include ``%(run_source)s`` in its format — see ``lovia web``'s
    logging setup for the wiring.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        tag = "/".join(p for p in (RUN_SOURCE.get(), CURRENT_AGENT.get()) if p)
        # A control char in a free-form agent name would split the one-line
        # prefix and fabricate log lines.
        if tag and not tag.isprintable():
            tag = " ".join(tag.split())
        record.run_source = f"[{tag}] " if tag else ""
        return True


def run_source_log_filter() -> logging.Filter:
    """A handler filter exposing the current run's source as ``%(run_source)s``."""
    return _RunSourceFilter()


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
    buffered: list[events.Event | LegFrame]
    subscription: _Subscription
    status: str


class RunController:
    """Owns a run's supervised task, cancel token, mailbox, event hub, and the
    snapshot mirror used to serve late subscribers.

    The snapshot state (``history_runs``/``completed_mirror``/
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
        agent_name: str,
        first_input: str | list[Message],
        first_checkpoint: CheckpointOptions | None,
        seed_entries: list[TranscriptEntry],
        is_new: bool,
        title_message: str | None,
        source: str,
        mailbox: Mailbox | None = None,
        client: JsonObject | None = None,
        run_deps: Any = None,
        await_approvals: bool = True,
        chat_created_at: float | None = None,
    ) -> None:
        self.deps = deps
        self.supervisor = supervisor
        self.session_id = session_id
        self.agent = agent
        # The agent as the run was handed it, before the chat's workspace was
        # bound in: what ``RunSupervisor.retune`` rebuilds for another mode.
        self.source_agent = agent
        # The registry key the agent is served under, resolved by the caller
        # *before* the workspace binding copied the agent (``name_of`` looks
        # up by identity, and a copy would fall back to ``agent.name``).
        self.agent_name = agent_name
        # The agent whose turn is running: the entry agent until a handoff.
        self.active_agent: Agent[Any] = agent
        self.source = source
        # False for a turn whose caller can't answer an approval (a blocking
        # /api/chat): the run doesn't park, Runner.run's order decides.
        self.await_approvals = await_approvals
        # Set by a resume a person's message asked for: the resumed leg gets
        # its turn and budget limits afresh (see _counted_from).
        self.fresh_allowance = False
        # The page's client context and what run_deps= built from it: the
        # run's RunContext.deps, shared by every auto-chained leg. The client
        # is persisted beside each leg's checkpoint pointer for a resume.
        self.client = client
        self.run_deps = run_deps
        self.cancel = CancelToken()
        # Caller-supplied for runs steered from outside the HTTP surface
        # (a parent's send_to_subagent pushes into the same channel the
        # /inject endpoint uses); otherwise this run's own.
        self.mailbox = mailbox if mailbox is not None else Mailbox()
        self.hub = EventHub()
        # The current leg's checkpoint id: a fresh one per auto-chained leg,
        # None without a checkpointer.
        self.run_id = (
            first_checkpoint.resolved_run_id if first_checkpoint is not None else None
        )
        # The whole run's id, from its first leg to its end and across a
        # resume: its durable record, lifecycle events and GET /api/runs.
        # start()/start_resume() settle it before anyone can see the run.
        self.record_id = self.run_id or uuid.uuid4().hex
        self.status = "running"
        self.turns = 0
        self.succeeded = False
        self.final_output: Any = None
        # Terminal outcome, set exactly once in the task's finally — lets a
        # programmatic owner (e.g. the subagent runner) await ``task`` and
        # read what happened without re-querying the run-record store.
        self.final_status: str | None = None
        self.final_error: str | None = None
        # The exception behind ``final_error``, when one ended the run.
        self.final_exception: Exception | None = None
        self.started_at = time.time()
        # Where a snapshot's untimed (pre-timing) history starts from.
        self.chat_created_at = chat_created_at or self.started_at
        # The directory and permission mode this run works under (a None
        # mode: the agent's own policy) — moved by a switch it can take
        # (``RunSupervisor.retune``). The chats it creates — a subagent task,
        # a spawned chat — work the same way.
        self.binding = ChatBinding(None, None)
        # Whose remembered approvals answer this run's, and under which agent
        # key (``RouterDeps.grant_owner``): a task's are its chat's.
        self.grant_owner: tuple[str, str | None] = (session_id, None)
        # Cumulative usage across auto-chained legs, for the durable run record.
        self.usage = Usage()
        # Final model call's prompt size (context fill), from the last leg that
        # reported one — rides in the run record next to the cumulative usage.
        self.last_input_tokens: int | None = None
        # The window the last model call was budgeted against, for the ring.
        self.context_window: int | None = None
        # snapshot mirror (task-private until read synchronously by attach)
        # The session history, by run: a snapshot names its user turns and
        # times its messages from it.
        self.history_runs: Runs = []
        self.completed_mirror: list[TranscriptEntry] = []
        # The leg the mirror shows — trails ``run_id`` across an auto-chain hop
        # until the next leg resets the mirror; None before the first leg.
        self._mirror_run_id: str | None = None
        self.current_turn_entries: list[TranscriptEntry] = []
        self.in_flight_buffer: list[events.Event | LegFrame] = []
        self.pending_approval: ApprovalAsked | None = None
        # The call id a mode switch settled (recheck_pending): its card says
        # the permission mode decided, not a person.
        self._policy_settled: str | None = None
        # A resume's answers to the cards its run was suspended on, by call
        # id: the resumed run asks again (core judges a dangling call afresh)
        # and is answered from here, once.
        self._answers: dict[str, Approval] = {}
        # The card the run suspended on, once nobody answered it in time: the
        # record keeps it, so the chat can offer it with no run attached.
        self._suspended: ApprovalAsked | None = None
        # A subagent task's run (its chat has a parent): a shutdown stops it
        # rather than suspending it, since the run awaiting it ends too.
        self.is_task = False
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
        # The write of what a stop froze (see freeze); None until then.
        self._frozen: asyncio.Future[None] | None = None
        self.task: asyncio.Task[None] | None = None

    # -- lifecycle ------------------------------------------------------- #

    def _ensure_begun(self) -> None:
        """Create the supervised task on the first subscribe, so the very first
        events (RunStarted/early deltas) can never be published before a
        subscriber exists."""
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    def subscribe_live(self) -> _Subscription:
        """Subscribe to a fresh run (START) and begin it. No snapshot — a first
        turn has no history-so-far, and the client already drew the user's own
        message."""
        sub = self.hub.subscribe()
        self._ensure_begun()
        return sub

    def subscribe_resume(self, runs: Runs) -> _Attachment:
        """Subscribe to a resumed run, opening with ``runs`` as its snapshot.

        A resume differs from a START in having a past: the interrupted turn is
        already on screen, and the run is about to re-announce the tool calls it
        drains (``_drain_pending_calls``). Sending the snapshot lets the client
        treat this exactly like a re-attach — continue the run's own bubble, and
        recognise the replayed calls instead of drawing them twice.

        ``runs`` comes from the caller (session history + the checkpoint's
        own), NOT from ``history_runs``/``completed_mirror``: those are
        task-private by design and still empty here, since the task has not
        begun. Subscribe first, then begin, so no early event is missed.
        """
        sub = self.hub.subscribe()
        attachment = _Attachment(
            snapshot=view_messages(
                runs, created_at=self.chat_created_at, updated_at=time.time()
            ),
            buffered=[],  # nothing has been published yet — the tail IS the run
            subscription=sub,
            status=self.status,
        )
        self._ensure_begun()
        return attachment

    def attach(self, *, with_snapshot: bool) -> _Attachment:
        """Re-attach to a live run: authoritative snapshot + current-turn replay
        + live tail, captured atomically against the hub seq."""
        sub = self.hub.subscribe()
        buffered = list(self.in_flight_buffer)
        if with_snapshot:
            # This run's turns get their refs when it finishes (the refs
            # frame), not before.
            runs = [*self.history_runs, Run(None, self.completed_mirror)]
            snapshot = view_messages(
                runs, created_at=self.chat_created_at, updated_at=time.time()
            )
        else:
            snapshot = []
        # Belt-and-suspenders: a parked approval is in the buffer, but re-add it
        # if it somehow isn't, so a late client can always decide.
        if self.pending_approval is not None and self.pending_approval not in buffered:
            buffered = [*buffered, self.pending_approval]
        return _Attachment(
            snapshot=snapshot, buffered=buffered, subscription=sub, status=self.status
        )

    def inject(self, content: InjectedContent) -> int:
        return self.mailbox.push(content)

    def uninject(self, token: int) -> bool:
        return self.mailbox.remove(token)

    def cancel_run(self, *, user: bool, reason: str | None = None) -> None:
        self._user_cancelled = self._user_cancelled or user
        if user and not self._discard_partial:
            self.freeze()
        # The reason is what the run's logs say stopped it (the loop's
        # ``run.interrupted`` line); the record keeps the stable "cancelled".
        self.cancel.cancel(reason or (USER_STOP if user else "server shutdown"))
        # Unblock a run parked on a pending approval (deny) so it can wind down.
        self.deps.approvals.deny_pending(self.session_id)
        # Same for a run parked on an ask_human question (cancel).
        if self.deps.questions is not None:
            self.deps.questions.cancel_session(self.session_id)

    async def recheck_pending(self) -> None:
        """Judge the request the run waits on again, by the permissions as
        they are now (a mode switch). One the new mode decides is settled
        here — allowed, it runs; denied, the model is told why — the way a
        person's answer is: claimed first, so a click on its card and this
        never both decide. One the mode still asks about keeps its card."""
        asked = self.pending_approval
        if asked is None:
            return
        verdict = await asked.request.recheck()
        call_id = asked.request.call.id
        if (
            verdict.action == "ask"
            or self.pending_approval is not asked
            or not self.deps.approvals.claim(self.session_id, call_id)
        ):
            return
        allowed = verdict.action == "allow"
        self._policy_settled = call_id
        reason = "" if allowed else verdict.reason or "denied by the permission mode"
        await self.deps.approvals.resolve(
            self.session_id, call_id, Approval(allowed, reason)
        )

    def _set_status(self, status: str) -> None:
        """Change the live status and say so on ``/api/events``: a chat's page
        shows its tasks' pending approvals (``RunInfo.pending_approval``) and
        must learn when one starts or stops waiting."""
        self.status = status
        self.deps.emit(
            "run_status",
            RunStatusData(
                session_id=self.session_id, run_id=self.record_id, status=status
            ),
        )

    def grant_context(self) -> str:
        """Where this run's approvals are given: the *active* agent (a handoff
        target after a handoff) and its workspace root. The entry agent is
        named by its registry key — a task's by its chat's, whose approvals
        it shares; a handoff target by its own name, so a grant given to it
        applies only while it is the one acting."""
        active = self.active_agent
        if active is not self.agent:
            return agent_grant_context(self.deps.name_of(active), active)
        owner, owner_agent = self.grant_owner
        if owner == self.session_id or owner_agent is None:
            owner_agent = self.agent_name  # its own approvals
        return agent_grant_context(owner_agent, active)

    async def _granted(self, ev: events.ApprovalRequired) -> bool:
        """Whether remembered approvals cover every one of ``ev.scopes``."""
        if not ev.scopes:
            return False
        try:
            return await self.deps.store.has_grants(
                self.grant_owner[0], ev.scopes, self.grant_context()
            )
        except Exception as exc:  # noqa: BLE001 — a lookup failure asks, never allows
            log.warning("grant lookup failed for %s: %s", self.session_id, exc)
            return False

    async def _await_approval(
        self, ev: events.ApprovalRequired
    ) -> ApprovalResolved | None:
        """Await the HTTP decision for ``ev``; returns how it was settled, for
        the clients' cards, or None when nobody answered within
        ``suspend_after`` and the run suspends on it.

        Without the deadline a clientless (scheduled) run parked on an approval
        holds its concurrency slot forever — ``RunBudget.max_seconds`` is only
        checked at turn boundaries, which a parked run never reaches.
        """
        # Visible before the wait, not only after a timeout: a clientless run
        # (schedule fire, subagent task) parked here is otherwise a silent
        # stall of up to ``suspend_after`` seconds.
        after = self.deps.suspend_after
        log.info(
            "run parked on approval: session=%s tool=%s (%s)",
            self.session_id,
            ev.call.name,
            "waits for a decision" if after is None else f"suspends after {after:.0f}s",
        )
        # ``await_decision`` registers the pending future in its first step,
        # before it yields — so awaiting the coroutine directly leaves no
        # window in which a concurrent ``cancel_run()`` → ``deny_pending()``
        # could run before registration and miss it (a task spawned via
        # ensure_future would open exactly that window). The deadline is a
        # timer that *suspends* the pending approval rather than cancelling
        # the awaiter: the run stops at the call to be resumed, not stopped.
        # (A tab's claim already in flight wins: the timer then finds nothing.)
        decision = self.deps.approvals.await_decision(self.session_id, ev)
        timer = (
            None
            if after is None
            else asyncio.get_running_loop().call_later(
                after, self.deps.approvals.suspend_pending, self.session_id
            )
        )
        try:
            approval = await decision
        finally:
            if timer is not None:
                timer.cancel()
        if isinstance(approval, _Suspension):
            log.info(
                "approval %s for session %s unanswered after %.0fs: run suspended",
                ev.call.id,
                self.session_id,
                after or 0,
            )
            return None
        # A sweep that found the approval still unclaimed decided it: a stop
        # (deny_pending) or a mode switch (recheck_pending). Anything else was
        # a tab's answer.
        cause: Literal["user", "cancel", "policy"] = (
            "cancel"
            if self.cancel.is_cancelled and not approval.approved
            else "policy"
            if self._policy_settled == ev.call.id
            else "user"
        )
        # Spent: a provider may reuse the call id for a later request.
        self._policy_settled = None
        return ApprovalResolved(ev.call.id, approval.approved, cause)

    def freeze(self) -> None:
        """Fold what the user has seen of this leg into the durable Session.

        On a user stop (and a non-resumable failure) the chat that a reload or
        a follow-up message reads must be the one on screen — now, not once the
        run reaches its next safe point, which for a mid-flight tool is only
        after that tool returns. So the entries are captured synchronously,
        here; only the write is deferred, to ``_frozen``, which
        :meth:`RunSupervisor.settle` and the task's ``finally`` await.

        Keyed by the leg's checkpoint ``run_id``: ``Session.append`` ignores a
        stored one, and the task pulls no further event until this write lands,
        so a run finishing in the background anyway (a stop during its final
        model turn) can neither store its version first nor add a second copy.
        Without a checkpointer the loop would mint its own id, so nothing is
        folded.
        """
        if self._frozen is not None or self.deps.session is None:
            return
        run_id, entries = self._seen()
        if run_id is None or not entries:
            return
        # The pointer names the current leg — past the mirror's on an
        # auto-chain hop, the leg the stop kept from starting.
        self._frozen = asyncio.ensure_future(
            self._write_frozen(run_id, entries, time.time(), self.run_id)
        )

    def _seen(self) -> tuple[str | None, list[TranscriptEntry]]:
        """The current leg's ``run_id`` and its transcript as shown: the
        finished turns, the running turn so far, the model call still
        streaming, and an interrupted result for each unanswered call."""
        if self._mirror_run_id is None:
            # The task has not opened its first leg: the input is all there is.
            run_id, entries = self.run_id, list(self._seed_entries)
        else:
            run_id = self._mirror_run_id
            entries = [*self.completed_mirror, *self.current_turn_entries]
        # A model call's deltas become entries only with its MessageCompleted;
        # the ones after the turn's last such frame are a call still streaming.
        text: list[str] = []
        reasoning: list[str] = []
        for ev in self.in_flight_buffer:
            if isinstance(ev, events.MessageCompleted):
                text, reasoning = [], []
            elif isinstance(ev, events.TextDelta):
                text.append(ev.delta)
            elif isinstance(ev, events.ReasoningDelta):
                reasoning.append(ev.delta)
        if reasoning:
            # No provider: an unsigned fragment is shown, never replayed.
            entries.append(ReasoningEntry(content="".join(reasoning)))
        if text:
            entries.append(AssistantTextEntry(content="".join(text)))
        # Unanswered calls are the newest turn's (a turn's next model call waits
        # for all its results), so their results go at the end. Paired by
        # occurrence, as core does: a provider may reuse an id across turns.
        # The tool may still be running: say so rather than claim it never ran.
        entries += [
            ToolResultEntry(call_id=call.id, output=_INTERRUPTED, is_error=True)
            for call in pending_tool_calls(entries)
        ]
        # The mirror holds MessageCompleted's copies, which keep the model's raw
        # arguments even for a call core rejected and rewrote. Session history
        # is replayed as is, so make every (now answered) call wire-safe here.
        return run_id, normalize_replayed_entries(entries)

    async def _write_frozen(
        self,
        run_id: str,
        entries: list[TranscriptEntry],
        stopped_at: float,
        pointer: str | None,
    ) -> None:
        # Timed like a segment the loop writes, when the checkpoint knew the
        # leg's start. Best effort, like the run record: a failed write must
        # not break the stop, nor the task's finally that awaits it.
        # Once saved, the run's reconnect pointer goes too — for every way a
        # run is stopped, not only the HTTP cancel's eager clear — or a reload
        # would splice its checkpoint (still there until the run winds down)
        # in after the same turns, and offer to resume a stopped run.
        store, session = self.deps.store, self.deps.session
        assert session is not None  # freeze() checked
        try:
            snapshot = (
                await store.checkpointer.load(run_id)
                if store.checkpointer is not None
                else None
            )
            meta: JsonObject = {}
            if snapshot is not None and snapshot.started_at is not None:
                meta[TIMING_META_KEY] = {
                    "started_at": snapshot.started_at,
                    "finished_at": stopped_at,
                }
            if snapshot is not None and snapshot.context_state:
                # The context policy's carried state (calibration + sticky
                # decisions), as a finished run's segment has it: the next run
                # in this chat seeds from the latest segment, and without it
                # would estimate uncalibrated and re-derive every decision.
                meta[STATE_META_KEY] = snapshot.context_state
            await session.append(
                self.session_id, entries, run_id=run_id, meta=meta or None
            )
            if store.checkpointer is not None and pointer:
                await store.clear_active_run_id(self.session_id, expected=pointer)
        except Exception as exc:  # noqa: BLE001 — logged; the stop itself stands
            log.warning("could not save stopped run %s: %s", run_id, exc)

    # -- snapshot mirror ------------------------------------------------- #

    def _ingest(self, ev: events.Event) -> None:
        # A retry or compacting notice is current only until the next event,
        # so the buffer holds at most the latest one, as its tail: a reconnect
        # during the backoff (or the summary) shows it, one after it doesn't.
        if self.in_flight_buffer and isinstance(
            self.in_flight_buffer[-1], (events.ModelRetrying, events.ContextCompacting)
        ):
            self.in_flight_buffer.pop()
        if isinstance(ev, events.TurnStarted):
            self.turns = ev.turn
            self.active_agent = ev.agent
            self.in_flight_buffer = []
            self.current_turn_entries = []
        elif isinstance(ev, events.HandoffOccurred):
            self.active_agent = ev.to_agent
        if isinstance(
            ev,
            (
                events.TurnStarted,
                events.TextDelta,
                events.ReasoningDelta,
                events.OutputDiscarded,
                events.ModelRetrying,
                events.MessageCompleted,
                events.ToolCallStarted,
                events.ToolCallCompleted,
                # (an ApprovalRequired goes out as an ApprovalAsked frame)
                events.UserMessageInjected,
                events.HandoffOccurred,
                events.ContextCompacted,
                events.ToolCallFailed,
                events.RunCompleted,
                events.RunFailed,
            ),
        ) or (isinstance(ev, events.ContextCompacting) and ev.active):
            self.in_flight_buffer.append(ev)
        if isinstance(ev, events.UserMessageInjected):
            self.current_turn_entries.append(
                InputEntry(role="user", content=ev.content)
            )
        elif isinstance(ev, events.MessageCompleted):
            self.current_turn_entries.extend(ev.entries)
        elif isinstance(ev, events.ToolCallCompleted):
            # ``ev.result`` is the tool's raw return value — for a
            # parts-returning tool that IS the parts list, so the mirror
            # carries them and the tool-images route can serve a live run's
            # images before any checkpoint persists the real entry.
            self.current_turn_entries.append(
                ToolResultEntry(
                    call_id=ev.call.id,
                    output=ev.output,
                    is_error=ev.is_error,
                    parts=None if ev.is_error else coerce_parts(ev.result),
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

    def open_call(self) -> ToolCall | None:
        """The tool call under way, the newest of several; None between calls."""
        started: list[ToolCall] = []
        for ev in self.in_flight_buffer:
            if isinstance(ev, events.ToolCallStarted):
                started.append(ev.call)
            elif isinstance(ev, events.ToolCallCompleted):
                started = [call for call in started if call.id != ev.call.id]
        return started[-1] if started else None

    def _publish(self, ev: events.Event) -> None:
        if isinstance(ev, events.ToolCallCompleted) and isinstance(
            ev.result, (ProcessStart, ProcessOutput)
        ):
            started = (
                ev.result.process_id if isinstance(ev.result, ProcessStart) else ""
            )
            self.deps.processes.mark(
                self.session_id, started=started, title=_described(ev.call.arguments)
            )
        self._ingest(ev)
        self.hub.publish(ev)

    def _publish_frame(self, frame: LegFrame) -> None:
        # Buffered like the run's own events: a re-attach replays the card
        # and how it was settled, so it never shows a decided card as open.
        self.in_flight_buffer.append(frame)
        self.hub.publish(frame)

    def announce(self, frame: MCPUnavailable) -> None:
        """A frame from outside the run's events — a plugin saying something
        to the chat as the run starts — sent and kept like the run's own."""
        self._publish_frame(frame)

    def _publish_refs(self, entries: list[TranscriptEntry]) -> None:
        """Name the finished leg's user turns (its segment is keyed by run_id).

        Numbered from the run's own canonical ``entries``: the mirror misses
        user entries no event announces, like an output-repair prompt. The
        client stamps the frame onto its bubbles by position, so it is sent
        only when those are the turns the mirror — what streamed — shows.
        """
        refs = user_turn_refs([Run(self.run_id, entries)])
        shown = sum(
            1
            for e in self.completed_mirror
            if isinstance(e, InputEntry) and e.role == "user"
        )
        if len(refs) == shown:
            frame = UserTurnRefs([r for r in refs if r])
            # Replayed with the leg's RunCompleted to a reconnect before this
            # leg is cleared away: its snapshot leaves these turns unnamed.
            self.in_flight_buffer.append(frame)
            self.hub.publish(frame)

    async def _publish_timing(self) -> None:
        """Send the just-finished leg's timing, read back from the segment the
        loop wrote before its ``RunCompleted`` — the very figures a replay
        shows, so a live answer and a reloaded one agree. Nothing when that
        segment went untimed (resumed from a checkpoint that predates timing).

        Best-effort: the answer is already saved, so a failed read-back only
        costs the live footer its duration — never the run its success.
        """
        session = self.deps.session
        if session is None:
            return
        try:
            segments = await session.segments(self.session_id)
        except Exception as exc:
            log.warning("timing read failed for session %s: %s", self.session_id, exc)
            return
        # Keyed by run_id when checkpointing; else the store minted one, and
        # this leg's segment is the newest.
        seg = next(
            (s for s in reversed(segments) if s.run_id == self.run_id),
            None if self.run_id else (segments[-1] if segments else None),
        )
        timing = segment_timing(seg.meta) if seg is not None else None
        if timing is None:
            return
        start, finish = timing
        frame = LegTiming(finished_at=finish, duration=max(0.0, finish - start))
        self.in_flight_buffer.append(frame)  # as for refs: a reconnect replays it
        self.hub.publish(frame)

    # -- the supervised task --------------------------------------------- #

    async def _run(self) -> None:
        RUN_SOURCE.set(self.source)  # tag every log line under this task
        deps, sid = self.deps, self.session_id
        session, store = deps.session, deps.store
        UI_DATA_SINK.set((store, sid))
        RUN_CLIENT.set(self.client or None)
        RUN_SERVER.set(deps)
        RUN_PROJECT.set(deps.project_of(self.binding))
        next_input: str | list[Message] = self._first_input
        ckpt = self._first_ckpt
        seed = self._seed_entries
        title_args: tuple[str, str, Any, str] | None = None
        succeeded = False
        error_seen = False
        final_error: str | None = None  # message for the durable run record
        failed_terminally = False  # ended in a non-resumable failure (drop checkpoint)
        # Set when the run suspended on a card: what its record waits for.
        waiting_for: JsonObject | None = None
        # The durable run record, under the run's stable id — so a resumed run
        # finalizes the row it opened. Best-effort bookkeeping: never let it
        # break the run itself.
        record_id = self.record_id
        try:
            await store.start_run(
                RunRow(
                    id=record_id,
                    session_id=sid,
                    agent=self.agent_name,
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
        log.info("run started: session=%s agent=%s", sid, self.agent_name)
        deps.emit(
            "run_started",
            RunStartedData(
                session_id=sid,
                run_id=record_id,
                agent=self.agent_name,
                source=self.source,
            ),
        )
        try:
            # A task's own limits (its Subagents plugin's), on every leg it
            # runs; any other chat runs on the server's.
            own = await store.run_limits(sid)
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
                segments = await session.segments(sid) if session is not None else []
                self.history_runs = segments_runs(segments)
                self.completed_mirror = list(seed)
                self._mirror_run_id = self.run_id
                self.current_turn_entries = []
                self.in_flight_buffer = []
                budget = (
                    replace(own.budget)
                    if own.budget is not None
                    else deps.fresh_budget()
                )
                max_turns = (
                    deps.max_turns_for(self.agent_name)
                    if own.max_turns is None
                    else own.max_turns
                )
                if self.fresh_allowance and ckpt is not None and ckpt.resume_from:
                    # A person's message continues the run: its limits count
                    # from where it stopped, not from its first leg.
                    max_turns += ckpt.resume_from.turns
                    budget = _counted_from(budget, ckpt.resume_from)
                handle = Runner.stream(
                    self.agent,
                    next_input,
                    deps=self.run_deps,
                    session=session,
                    session_id=sid,
                    context_policy=deps.context_policy,
                    cancel_token=self.cancel,
                    mailbox=self.mailbox,
                    max_turns=max_turns,
                    budget=budget,
                    retry=deps.retry,
                    tracer=deps.tracer,
                    checkpoint=ckpt,
                )
                succeeded = False
                error_seen = False
                async for ev in handle:
                    if self._frozen is not None:
                        # The run advances only as this loop pulls it: holding
                        # the next pull until a stop's write lands means a run
                        # finishing in the background can't store first.
                        await asyncio.shield(self._frozen)
                    if isinstance(ev, events.ApprovalRequired):
                        if not self.await_approvals:
                            # Left undecided, the call goes to the agent's
                            # approval_handler, else is denied.
                            continue
                        answer = self._answers.pop(ev.call.id, None)
                        if answer is not None:
                            # The card this run was suspended on, answered
                            # while it waited: asked again by the resume (core
                            # judges a dangling call afresh), settled here.
                            if answer.approved:
                                ev.approve()
                            else:
                                ev.reject(answer.reason)
                            self._publish_frame(
                                ApprovalResolved(ev.call.id, answer.approved, "user")
                            )
                            continue
                        if await self._granted(ev):
                            # Remembered: answer it here and publish a note in
                            # the card's place. Never for a deny — a verdict
                            # with a scope is an ask by construction.
                            ev.approve()
                            self._publish_frame(ApprovalAsked(ev, granted=True))
                            continue
                        if self.cancel.is_cancelled:
                            # A stop landed during the grant lookup, before the
                            # approval was registered for ``deny_pending`` to
                            # find: answer it here or the run parks forever.
                            ev.reject("the run was cancelled")
                            continue
                        # The loop gates on the consumer: do not advance the
                        # iterator until the decision lands. The awaiter is
                        # THIS task, not the HTTP request, so a detach can't
                        # release it. The card goes out with its deadline and
                        # is settled by a frame naming who decided — or, left
                        # unanswered, said to wait while the run suspends.
                        after = self.deps.suspend_after
                        asked = ApprovalAsked(
                            ev,
                            expires_at=None if after is None else time.time() + after,
                        )
                        self._publish_frame(asked)
                        self.pending_approval = asked
                        self._set_status("blocked_on_approval")
                        try:
                            resolved = await self._await_approval(ev)
                        finally:
                            self.pending_approval = None
                            self._set_status("running")
                        if resolved is None:
                            self._suspended = asked
                            self._publish_frame(Suspended(ev.call.id))
                        else:
                            self._publish_frame(resolved)
                        continue
                    if isinstance(ev, events.RunFailed):
                        error_seen = True
                        if isinstance(ev.error, RunSuspended):
                            if self._suspended is None and ev.error.call is not None:
                                # A tool put its answer off (an ask_human
                                # question nobody answered in time).
                                self._publish_frame(Suspended(ev.error.call.id))
                            if self._suspended is not None or ev.error.call:
                                # Not a failure: the run waits on that call.
                                # It ends just below.
                                continue
                    if isinstance(ev, events.RunCompleted) and self.run_id:
                        self._publish_refs(ev.result.entries)
                    if isinstance(ev, events.RunCompleted):
                        await self._publish_timing()
                    if isinstance(ev, events.MessageCompleted) and ev.context_window:
                        self.context_window = ev.context_window
                    self._publish(ev)
                    if isinstance(ev, events.RunCompleted):
                        succeeded = True
                        self.succeeded = True
                        self.final_output = ev.result.output
                        self.usage.add(ev.result.usage)
                        if ev.result.last_input_tokens is not None:
                            self.last_input_tokens = ev.result.last_input_tokens
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
                        self.agent_name,
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
                ckpt = await self.supervisor._checkpoint_for(
                    sid, uuid.uuid4().hex, self.client, record_id=record_id
                )
            if succeeded and store.checkpointer is not None:
                await store.clear_active_run_id(sid, expected=self.run_id)
        except RunSuspended as exc:
            # Suspended on a card nobody answered, or by a tool that put its
            # answer off: the checkpoint and its pointer stay (core classifies
            # this "interrupted"), and the record says what the run waits for.
            # A stop that landed with it wins.
            if self._user_cancelled:
                final_error = str(exc) or exc.__class__.__name__
                self.final_exception = exc
            elif self._suspended is not None:
                # The card no longer has a deadline: it waits for its answer.
                card = {**approval_data(self._suspended), "expires_at": None}
                waiting_for = {"kind": "approval", "request": cast(JsonObject, card)}
            elif exc.call is not None:
                # An ask_human question (the exception carries its text: it is
                # answered with POST /api/chat/answer), or another tool that
                # put its answer off (a message resumes it).
                request: JsonObject = {
                    "id": exc.call.id,
                    "name": exc.call.name,
                    "arguments": exc.call.arguments,
                }
                if exc.question is not None:
                    request["question"] = exc.question
                waiting_for = {
                    "kind": "question" if exc.question is not None else "call",
                    "request": request,
                }
            else:
                final_error = str(exc) or exc.__class__.__name__
                self.final_exception = exc
        except Exception as exc:
            # A terminal failure (MaxTurnsExceeded, provider error, …) that the
            # loop didn't already surface as an `error` event: synthesize one so
            # subscribers see a clear notice, mirroring the old drive_stream.
            log.warning("supervised run %s ended: %s", sid, exc)
            final_error = str(exc) or exc.__class__.__name__
            self.final_exception = exc
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
                # A user stop froze at cancel time; a failure freezes now. Either
                # way the write lands before the checkpoint goes, so the run is
                # never in neither place. ``_discard_partial``: being deleted.
                if failed_terminally and not self._discard_partial:
                    self.freeze()
                if self._frozen is not None:
                    await self._frozen
                await store.checkpointer.delete(rid)
                await store.clear_active_run_id(sid, expected=rid)
            if waiting_for is not None and self._user_cancelled:
                waiting_for = None  # a stop landed meanwhile: it wins
            if waiting_for is not None:
                # What the run had not read yet goes with it: a resume puts it
                # back in the mailbox. Recorded before the run leaves the
                # registry, so no start can slip in while the chat looks idle.
                # This is the record's one write: a report queued for it from
                # here on goes straight to the row (queue_for_waiting).
                waiting_for["queued"] = [
                    _queued_entry(content) for content in self.mailbox.drain()
                ]
                try:
                    await store.finish_run(
                        record_id,
                        status="waiting",
                        usage=usage_dict(
                            self.usage,
                            last_input_tokens=self.last_input_tokens,
                            context_window=self.context_window,
                        )
                        if self.usage.total_tokens
                        else None,
                        waiting_for=waiting_for,
                    )
                    # A message injected while that write was under way, into
                    # a run that will not read it: it waits with the run too.
                    # (No await between the last drain and the eviction below.)
                    while late := self.mailbox.drain():
                        for content in late:
                            await store.queue_for_waiting(
                                record_id, _queued_entry(content)
                            )
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("run record write failed for %s: %s", sid, exc)
                self.supervisor._kept[sid] = Kept(
                    self.source_agent, self.mailbox, self.source, self.is_task
                )
            await deps.approvals.release(sid)
            self.hub.close()
            self.supervisor._evict(sid, self)
            if title_args is not None:
                deps.schedule_title(*title_args)
            if self._user_cancelled or (not succeeded and final_error is None):
                # Stable outcome text for every cooperative stop: a user cancel
                # surfaces as RunCancelled ("user requested stop"), a shutdown
                # reaches here with no exception at all — both must read
                # "cancelled", not whatever the exception said.
                final_error = "cancelled"
            if self._user_cancelled:
                # Even if the run finished in the background (a stop during its
                # final model turn): the chat keeps what was frozen, not that.
                status = "cancelled"
            elif succeeded:
                status = "completed"
            elif waiting_for is not None:
                # Answering its card resumes it (start_run flips the row back).
                status = "waiting"
            elif failed_terminally:
                status = "failed"
            else:
                # Cooperative shutdown or a resumable failure: the checkpoint
                # survives, so a reconnect may pick this run back up — in which
                # case start_run flips this same row back to "running".
                status = "interrupted"
            error = None if status in ("completed", "waiting") else final_error
            self.final_status = status
            self.final_error = error
            # The code the run's `error` frame carried, for a chat opened later.
            error_code = (
                run_error_code(self.final_exception)
                if status in ("failed", "interrupted") and self.final_exception
                else None
            )
            try:
                # A waiting record was written above, before the eviction; a
                # second write here would drop what was queued for it since.
                if status != "waiting":
                    await store.finish_run(
                        record_id,
                        status=status,
                        error=error,
                        error_code=error_code,
                        usage=usage_dict(
                            self.usage,
                            last_input_tokens=self.last_input_tokens,
                            context_window=self.context_window,
                        )
                        if self.usage.total_tokens
                        else None,
                    )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("run record write failed for %s: %s", sid, exc)
            log.info(
                "run finished: session=%s status=%s%s",
                sid,
                status,
                f" error={error}" if error else "",
            )
            deps.emit(
                "run_finished",
                RunFinishedData(
                    session_id=sid,
                    run_id=record_id,
                    status=status,
                    error=error,
                    source=self.source,
                ),
            )


# --------------------------------------------------------------------------- #
# RunSupervisor — process-wide registry (one controller per session)
# --------------------------------------------------------------------------- #


def _spent_before(record: JsonObject | None, leg: Usage) -> Usage:
    """What a resumed run's earlier legs spent: its record's usage, written
    when it was interrupted, less what the interrupted leg had spent then —
    the leg reports that again, in its own total, when it completes."""
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    )
    return Usage(
        **{
            f: max(0, int(cast(int, (record or {}).get(f) or 0)) - getattr(leg, f))
            for f in fields
        }
    )


def _counted_from(budget: RunBudget | None, snapshot: RunSnapshot) -> RunBudget | None:
    """``budget`` with every limit raised by what ``snapshot``'s run already
    spent: a resumed run carries its spend (tokens, tool calls, seconds), so
    this leaves the whole allowance for what comes after."""
    if budget is None:
        return None

    def more(limit: int | float | None, spent: int | float) -> Any:
        return None if limit is None else limit + spent

    used = snapshot.usage
    return replace(
        budget,
        max_input_tokens=more(budget.max_input_tokens, used.input_tokens),
        max_output_tokens=more(budget.max_output_tokens, used.output_tokens),
        max_total_tokens=more(budget.max_total_tokens, used.total_tokens),
        # As the loop carries it: pending calls count again when they replay.
        max_tool_calls=more(
            budget.max_tool_calls,
            max(0, snapshot.tool_calls - len(pending_tool_calls(snapshot.entries))),
        ),
        max_seconds=more(budget.max_seconds, snapshot.elapsed),
    )


class RunSupervisor:
    def __init__(self, deps: RouterDeps) -> None:
        self.deps = deps
        self._controllers: dict[str, RunController] = {}
        # Per-chat start locks (see start/start_resume); weak, so a chat's
        # lock is gone once no start holds or waits on it.
        self._starting: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # Cancelled runs whose task is still winding down. Cancellation is
        # cooperative — the run only stops at its next safe point, which for a
        # mid-flight tool is after that tool returns — while ``cancel`` evicts
        # the controller immediately so the next message starts a fresh run.
        # Anything that must not race the wind-down's terminal persist waits
        # here; see :meth:`drain`. A *set* per session, not one controller: a
        # session whose replacement run is stopped just as fast has two runs
        # winding down at once, and both can still write.
        self._draining: dict[str, set[RunController]] = {}
        # The client context of each session's latest run, for the runs that
        # session starts on nobody's request: its subagent tasks, and the
        # report runs that deliver their results back into it.
        self.last_client: dict[str, JsonObject] = {}
        # What a run suspended on a card resumes with in this process: the
        # agent it was handed (a subagent task's is an unregistered clone,
        # which no registry lookup finds) and its mailbox (a task's is shared
        # with the run that spawned it). A restart loses it; a chat's run
        # then resumes by the registry, a task's is settled at startup.
        self._kept: dict[str, Kept] = {}
        # Set when a controller registers for a session, so a caller following
        # that session's run across suspensions (see :meth:`follow`) wakes.
        self._registered: dict[str, asyncio.Event] = {}

    @property
    def max_background_runs(self) -> int:
        return self.deps.max_background_runs

    def kept(self, session_id: str) -> Kept | None:
        """What the session's suspended run resumes with here, if this
        process suspended it."""
        return self._kept.get(session_id)

    def forget(self, session_id: str) -> None:
        """Drop what a suspended run was kept with: it was stopped, or its
        chat deleted. Wakes whoever follows it (:meth:`follow`)."""
        self._kept.pop(session_id, None)
        if (event := self._registered.pop(session_id, None)) is not None:
            event.set()

    def forget_all(self) -> None:
        """:meth:`forget` every suspended run (every chat was deleted)."""
        for session_id in [*self._kept, *self._registered]:
            self.forget(session_id)

    def _register(self, session_id: str, ctrl: RunController) -> None:
        self._controllers[session_id] = ctrl
        self._kept.pop(session_id, None)
        if (event := self._registered.pop(session_id, None)) is not None:
            event.set()

    async def follow(
        self, session_id: str, ctrl: RunController
    ) -> RunController | None:
        """Wait for ``ctrl``'s run to end, following it across suspensions:
        the controller it finally ended with, or None when it was stopped (or
        its chat deleted) while it waited on a card."""
        from .waiting import find_waiting

        try:
            while True:
                if ctrl.task is not None:
                    await asyncio.wait([ctrl.task])
                if ctrl.final_status != "waiting":
                    return ctrl
                while True:
                    event = self._registered.setdefault(session_id, asyncio.Event())
                    # Its resume runs under the same record; any other run in
                    # the chat is a new one, started after it was stopped.
                    live = self.get(session_id)
                    if live is not None and live.record_id == ctrl.record_id:
                        if live is not ctrl:
                            ctrl = live
                            break
                    rows = await self.deps.store.list_runs(
                        run_id=ctrl.record_id, limit=1
                    )
                    status = rows[0].status if rows else None
                    if status == "waiting":
                        if await find_waiting(self.deps, session_id) is None:
                            return None  # its checkpoint went: settled
                    elif status != "running":
                        return None  # stopped, or its chat deleted
                    # "running" with no controller: a resume is registering.
                    # Woken by a register or a stop (forget); the timeout is a
                    # safety net for a path that settles it without either.
                    # asyncio's: not the builtin before Python 3.11.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(event.wait(), timeout=5.0)
        finally:
            self._registered.pop(session_id, None)

    def get(self, session_id: str) -> RunController | None:
        return self._controllers.get(session_id)

    def __iter__(self) -> Any:
        return iter(list(self._controllers.items()))

    async def _checkpoint_for(
        self,
        sid: str,
        run_id: str,
        client: JsonObject | None,
        *,
        record_id: str | None = None,
    ) -> CheckpointOptions | None:
        """Point the chat at a leg's checkpoint ``run_id``; ``record_id`` names
        the run it continues (None: a first leg, whose record is ``run_id``)."""
        store = self.deps.store
        if store.checkpointer is None:
            return None
        await store.set_active_run_id(sid, run_id, context=client, record_id=record_id)
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
        input: str | list[Message],
        is_new: bool,
        title_message: str | None,
        autostart: bool = False,
        source: str = "user",
        mailbox: Mailbox | None = None,
        client: JsonObject | None = None,
        run_deps: Any = None,
        await_approvals: bool = True,
    ) -> RunController:
        """Register a run for ``session_id``; the caller begins it
        (``subscribe_live``), or ``autostart`` does.

        ``run_deps`` is the run's ``RunContext.deps``, built by the caller from
        ``client`` (the page's context) with :func:`build_run_deps` before
        it writes anything — so a refusal leaves no trace, and no deployer
        code runs between the checks here and the registration.

        ``await_approvals=False`` doesn't park the run on an approval: the
        agent's ``approval_handler`` decides, else the call is denied.
        """
        # A run stopped a moment ago must be in the history this one loads.
        await self.settle(session_id)
        # One start at a time per chat: binding the workspace may switch
        # the chat session's policy (set_policy), which a start about to
        # lose to a concurrent one must never do under the winner's run.
        async with self._start_lock(session_id):
            self._check_slot(session_id)
            # A run suspended on a card is still this chat's run: a fresh one
            # would take over its checkpoint pointer and lose it. Each starter
            # decides what to do instead (resume it, queue for it, skip).
            if await find_waiting(self.deps, session_id) is not None:
                raise WebError(
                    409,
                    "run_waiting",
                    "this chat's run waits on an approval",
                    hint="answer its approval card to resume it, or stop it",
                )
            meta = await self.deps.store.get(session_id)
            agent_name = self.deps.name_of(agent)  # before binding copies the agent
            grants = await self.deps.grant_owner(session_id)
            # The chat's directory, checked against the server's constraints,
            # and its permission mode and model, read now: a later mode switch
            # reaches the run through ``retune``, a model switch the next run.
            binding = self.deps.chat_binding(agent, meta)
            handed = agent
            agent = self.deps.effective_agent(agent, binding)
            agent = self.deps.on_chat_model(agent, meta.model if meta else None)
            # Every run of one web session shares that session's workspace session
            # (lazily opened here), so background processes survive across runs —
            # they die with the *chat* (deletion/shutdown), not with the turn.
            # Its policy reads the chat's attachment folder, never writes it
            # (a remote chat's: the host's copy, put there now).
            folder = await attachments_for_run(self.deps.store, session_id, agent)
            agent = await self.deps.workspaces.bind(session_id, agent, folder)
            # What a page attaching before the task opens its leg is shown —
            # one that follows the run_started event, from a run sent by
            # another client, gets there first: the task sets the same again.
            # Best effort: a history that won't load fails the run in its task,
            # as a failed run — raised here, it would stall a schedule's fire.
            history: Runs = []
            if self.deps.session is not None:
                try:
                    history = segments_runs(
                        await self.deps.session.segments(session_id)
                    )
                except Exception as exc:
                    log.warning("history prefill failed for %s: %s", session_id, exc)
            # Those awaits may have let a concurrent start for this chat in.
            # (Its workspace is this chat's too: nothing to undo.)
            self._check_slot(session_id, cap=False)
            client = dict(client or {})
            # ``input`` seeds the transcript for immediate display and is what the
            # runner consumes. A plain string is the text-only path; a list[Message]
            # (one ``user([...parts])``) carries composer attachments as content
            # parts, which input_to_entries preserves into the InputEntry.
            if isinstance(input, str):
                seed: list[TranscriptEntry] = (
                    [InputEntry(role="user", content=input)] if input else []
                )
            else:
                seed = input_to_entries(input) if input else []
            ctrl = RunController(
                deps=self.deps,
                supervisor=self,
                session_id=session_id,
                agent=agent,
                agent_name=agent_name,
                first_input=input,
                first_checkpoint=None,
                seed_entries=seed,
                is_new=is_new,
                title_message=title_message,
                source=source,
                mailbox=mailbox,
                client=client,
                run_deps=run_deps,
                await_approvals=await_approvals,
                chat_created_at=meta.created_at if meta else None,
            )
            ctrl.binding, ctrl.source_agent = binding, handed
            ctrl.grant_owner = grants
            ctrl.is_task = bool(meta and meta.parent_id)
            ctrl.history_runs = history
            ctrl.completed_mirror = list(seed)
            # Reserve the session slot BEFORE the checkpoint await: two concurrent
            # starts (e.g. two tabs submitting at once) would otherwise both pass the
            # caller's live-check and the second would silently orphan the first's
            # running task. The loser now gets the 409 above and attaches instead.
            self._register(session_id, ctrl)
            self.last_client[session_id] = client
        # The first leg's checkpoint id is the run's id too.
        ctrl.record_id = uuid.uuid4().hex
        try:
            ckpt = await self._checkpoint_for(session_id, ctrl.record_id, client)
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
        self,
        *,
        session_id: str,
        agent: Agent[Any],
        snapshot: RunSnapshot,
        client: JsonObject | None = None,
        run_deps: Any = None,
        answers: dict[str, Approval] | None = None,
        inbox: list[InjectedContent] | None = None,
        mailbox: Mailbox | None = None,
        source: str = "user",
        autostart: bool = False,
        await_approvals: bool = True,
        fresh_allowance: bool = False,
    ) -> RunController:
        """Register a resume of ``snapshot``; ``run_deps``, ``mailbox``,
        ``source``, ``autostart`` and ``await_approvals`` as for :meth:`start`.

        A run suspended on a card resumes with that card's answer in
        ``answers`` (by call id: the resumed run asks again, see
        ``RunController._answers``) and the messages it had not read yet,
        plus any new one, in ``inbox`` — read at its next turn.
        ``fresh_allowance``: a person's message asked for the resume, so its
        turn and budget limits count from here."""
        # One start at a time per chat: binding the workspace may switch
        # the chat session's policy (set_policy), which a start about to
        # lose to a concurrent one must never do under the winner's run.
        # A resume a person's message asked for is new work, held to the run
        # cap like a fresh start; a reconnect or an answer picks up old work.
        cap = fresh_allowance
        async with self._start_lock(session_id):
            self._check_slot(session_id, cap=cap)
            meta = await self.deps.store.get(session_id)
            agent_name = self.deps.name_of(agent)  # before binding copies the agent
            grants = await self.deps.grant_owner(session_id)
            # The chat's binding and model as they are now, like a fresh run's.
            binding = self.deps.chat_binding(agent, meta)
            handed = agent
            agent = self.deps.effective_agent(agent, binding)
            agent = self.deps.on_chat_model(agent, meta.model if meta else None)
            # Same chat-scoped workspace binding as start(): the resumed leg's
            # background processes are gone (they never survive a restart), but
            # its shell/file tools must land on the chat's one live session.
            folder = await attachments_for_run(self.deps.store, session_id, agent)
            agent = await self.deps.workspaces.bind(session_id, agent, folder)
            self._check_slot(session_id, cap=cap)  # as in start()
            client = dict(client or {})
            ckpt = CheckpointOptions(
                checkpointer=self.deps.store.checkpointer,
                resume_from=snapshot,
                delete_on_success=True,
            )
            seed = drop_system_entries(list(snapshot.entries))
            # A leg past the first checkpoints under its own id; the pointer beside
            # it names the run it continues.
            record_id = await self.deps.store.get_active_record_id(
                session_id, snapshot.run_id
            )
            records = await self.deps.store.list_runs(run_id=record_id, limit=1)
            # What a page attaching before the task opens its leg is shown (an
            # autostarted resume has no subscriber of its own): the task sets
            # the same again when it starts.
            history = (
                segments_runs(await self.deps.session.segments(session_id))
                if self.deps.session is not None
                else []
            )
            self._check_slot(session_id, cap=cap)  # those awaits, as above
            ctrl = RunController(
                deps=self.deps,
                supervisor=self,
                session_id=session_id,
                agent=agent,
                agent_name=agent_name,
                first_input=[],
                first_checkpoint=ckpt,
                seed_entries=seed,
                is_new=False,
                title_message=None,
                # The resumer's identity; if the run already has a record (it
                # normally does — same run_id), start_run keeps the original source.
                source=source,
                mailbox=mailbox,
                client=client,
                run_deps=run_deps,
                await_approvals=await_approvals,
                chat_created_at=meta.created_at if meta else None,
            )
            ctrl.fresh_allowance = fresh_allowance
            ctrl.is_task = bool(meta and meta.parent_id)
            ctrl.binding, ctrl.source_agent = binding, handed
            ctrl.grant_owner = grants
            ctrl.record_id = record_id
            ctrl.usage = _spent_before(
                records[0].usage if records else None, snapshot.usage
            )
            ctrl.history_runs = history
            ctrl.completed_mirror = list(seed)
            # The agent that was acting, a handoff target perhaps: what an
            # answer given before the task replays its turn (a grant written
            # for a suspended card) must be judged against.
            ctrl.active_agent = reachable_agents(agent).get(snapshot.agent_name, agent)
            ctrl._answers = dict(answers or {})
            for content in inbox or ():
                ctrl.mailbox.push(content)
            self._register(session_id, ctrl)
            self.last_client[session_id] = client
        if autostart:
            # Begun with no subscriber: a page attaches with reconnect.
            ctrl._ensure_begun()
        return ctrl

    async def retune(self, session_id: str) -> bool:
        """Bring the chat's live run onto the permission mode stored for it
        now. From the run's next tool call on, its workspace session judges
        by that mode's policy (:meth:`WorkspaceSessions.retune`), and what
        the run creates from here on — subagent tasks, spawned chats —
        inherits the mode. The tools the run was given stay as they were.

        False, with nothing changed, when no run is live or its workspace
        can't change in place (a ``build`` hook that varies more than the
        policy per mode): the mode then lands on the next run. Handoff
        targets open sessions of their own and keep the run's first mode.
        """
        # Under the start lock: a run starting meanwhile has read the mode
        # before this write, or reads it after — never binds half-way.
        async with self._start_lock(session_id):
            meta = await self.deps.store.get(session_id)
            ctrl = self._controllers.get(session_id)
            if ctrl is None or meta is None or ctrl.binding.root is None:
                return False
            mode = self.deps.effective_mode(meta.workspace_mode)
            binding = ChatBinding(ctrl.binding.root, mode)
            if binding == ctrl.binding:
                return True
            try:
                # From the agent as the run was handed it, not the bound one:
                # a remote chat's bound workspace is no local config to
                # rebuild, and the same build gives the session's own type.
                workspace = self.deps.effective_agent(
                    ctrl.source_agent, binding, hooks=False
                ).workspace
            except Exception as exc:  # noqa: BLE001 — the switch is stored; it lands next run
                log.warning(
                    "mode switch for %s waits for its next run: %s", session_id, exc
                )
                return False
            if workspace is None or not self.deps.workspaces.retune(
                session_id, workspace
            ):
                log.info("mode switch for %s waits for its next run", session_id)
                return False
            ctrl.binding = binding
            # The request it waits on was judged by the old mode.
            await ctrl.recheck_pending()
            return True

    def _start_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._starting.get(session_id)
        if lock is None:
            lock = self._starting[session_id] = asyncio.Lock()
        return lock

    def _check_slot(self, session_id: str, *, cap: bool = True) -> None:
        """Refuse a second run for one session, and (``cap``) one over the limit."""
        if session_id in self._controllers:
            raise WebError(
                409, "run_active", "a run is already active for this session"
            )
        if cap and len(self._controllers) >= self.max_background_runs:
            raise WebError(429, "too_many_runs", "too many concurrent runs")

    def cancel(
        self, session_id: str, *, discard: bool = False, reason: str | None = None
    ) -> bool:
        """Cancel the live run, if any. ``discard`` additionally drops the
        partial transcript instead of persisting it — for session deletion.
        ``reason`` names who stopped it, for the logs (default: a user stop)."""
        ctrl = self._controllers.get(session_id)
        if ctrl is None:
            return False
        # Evict synchronously so an immediate follow-up /stream starts fresh;
        # the task winds down + deletes its checkpoint in the background.
        self._controllers.pop(session_id, None)
        if ctrl.task is not None:
            self._draining.setdefault(session_id, set()).add(ctrl)
        ctrl._discard_partial = discard
        reason = reason or USER_STOP
        # One line per stop, whatever the run was doing: a run stopped
        # mid-model-call ends without a ``run.interrupted`` line to say why.
        log.info("run stop requested: session=%s (%s)", session_id, reason)
        ctrl.cancel_run(user=True, reason=reason)
        return True

    def stopping(self, session_id: str) -> bool:
        """Whether a stopped run of this session is still winding down — its
        record reads "cancelled" only once it has."""
        return bool(self._draining.get(session_id))

    async def settle(self, session_id: str) -> None:
        """Wait until this session's stopped runs have saved what they showed.

        One quick write each (see :meth:`RunController.freeze`) — unlike
        :meth:`drain`, which waits for the runs themselves to wind down.
        """
        for ctrl in list(self._draining.get(session_id, ())):
            if ctrl._frozen is not None:
                # Shielded: a caller giving up must not cancel the write.
                await asyncio.shield(ctrl._frozen)

    async def drain(
        self, session_id: str | None = None, *, timeout: float = 60.0
    ) -> bool:
        """Wait for this session's cancelled runs (every session's, given
        ``None``) to wind down. ``False`` on timeout.

        Returns once each stopped task's ``finally`` has folded its interrupted
        turn into the Session (and dropped its checkpoint) — the point after
        which the durable transcript matches what the client is looking at.
        Callers that rewrite history must go through here first, or they both
        read a transcript missing its last user turn and get that turn appended
        back underneath them a moment later.
        """
        deadline = time.monotonic() + timeout
        # Re-read ``_draining`` after every wait: nothing stops a client from
        # starting a replacement run (the session is free the moment its
        # predecessor is cancelled) and stopping that one too while we sit
        # here, and its tail can write just as late. Tracked by task so an
        # already-awaited one is never waited on twice — the loop consumes new
        # arrivals rather than spinning on settled ones.
        awaited: set[asyncio.Task[None]] = set()
        while True:
            groups = (
                self._draining.values()
                if session_id is None
                else [self._draining.get(session_id, set())]
            )
            tasks = [
                c.task
                for ctrls in groups
                for c in ctrls
                if c.task is not None and c.task not in awaited
            ]
            if not tasks:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # asyncio.wait never cancels what it waits on (unlike wait_for), so
            # a timeout here leaves the run winding down instead of killing it.
            _done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                return False
            awaited.update(tasks)

    def _evict(self, session_id: str, ctrl: RunController) -> None:
        if self._controllers.get(session_id) is ctrl:
            self._controllers.pop(session_id, None)
        draining = self._draining.get(session_id)
        if draining is not None:
            draining.discard(ctrl)
            if not draining:
                self._draining.pop(session_id, None)

    async def shutdown(self, *, grace: float = 10.0) -> None:
        from .waiting import find_waiting, stop_waiting

        ctrls = list(self._controllers.values())
        for c in ctrls:
            if c.pending_approval is not None and not c.is_task:
                # Parked on a card: suspend it, so the card outlives the
                # restart and answering it resumes the run. (A task stops: the
                # run that awaits it ends with this process.)
                self.deps.approvals.suspend_pending(c.session_id)
            else:
                c.cancel_run(user=False)  # cooperative stop; KEEP the checkpoint
        tasks = [c.task for c in ctrls if c.task is not None]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=grace)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        # A task already suspended goes the same way: nothing here would
        # resume it after the restart.
        for sid, kept in list(self._kept.items()):
            if kept.is_task and (waiting := await find_waiting(self.deps, sid)):
                with contextlib.suppress(Exception):
                    await stop_waiting(self.deps, waiting)


# --------------------------------------------------------------------------- #
# SSE bridge
# --------------------------------------------------------------------------- #


async def forward(
    source: _Attachment | _Subscription,
    *,
    sid: str,
    run_id: str,
) -> AsyncIterator[dict[str, str]]:
    """Stream a subscription (START/resume) or a re-attach to SSE payloads,
    opening with the ``session`` frame that names the chat and the run.

    On client disconnect, sse-starlette cancels this generator → the ``finally``
    unsubscribes (the entire **detach** mechanism); the run is never touched.
    """
    yield frame("session", SessionData(session_id=sid, run_id=run_id))
    if isinstance(source, _Attachment):
        sub = source.subscription
        yield frame(
            "snapshot",
            SnapshotData(
                session_id=sid,
                status=source.status,
                entries=[m.model_dump() for m in source.snapshot],
            ),
        )
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
