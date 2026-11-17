"""Runs suspended on an approval: what a waiting chat keeps, and how it resumes.

A run whose card nobody answers within ``suspend_after`` stops at the call
(core's :class:`~lovia.exceptions.RunSuspended`). Its checkpoint and the
chat's pointer to it stay; its run record reads ``waiting``, with
``waiting_for`` holding the card (``request``) and the messages the run had
not read yet (``queued``). No controller is live, so it holds no slot.

Everything that would start a run in such a chat comes through here instead
(``RunSupervisor.start`` refuses it with ``run_waiting``): answering the card
resumes the run with that answer, a message resumes it and joins it, a stop
settles it, and a subagent's report waits in its queue.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ..approvals import Approval
from ..checkpointer import RunSnapshot
from ..runtime.loop import pending_tool_calls
from ..runtime.resume import normalize_replayed_entries
from ..session import STATE_META_KEY, TIMING_META_KEY
from ..steering import InjectedContent
from ..transcript import InputEntry, ToolResultEntry, entry_from_dict
from ..types import JsonObject
from .api.serialization import drop_system_entries
from .errors import WebError
from .run_deps import RunTrigger, build_run_deps
from .sse import RunFinishedData
from .store import RunRow

if TYPE_CHECKING:
    from fastapi import Request

    from .api.deps import RouterDeps
    from .supervisor import RunController

log = logging.getLogger(__name__)

#: Checkpoint statuses a run can resume from.
RESUMABLE = ("interrupted", "running")

# The result a stop gives the call its run was suspended on.
_STOPPED = "Not run: the run was stopped while this call waited for approval."


@dataclass(frozen=True)
class Waiting:
    """A chat's suspended run: its record and the checkpoint it resumes from."""

    record: RunRow
    snapshot: RunSnapshot

    @property
    def session_id(self) -> str:
        return cast(str, self.record.session_id)

    @property
    def request(self) -> JsonObject:
        """The card the run waits on, as its ``approval_required`` frame."""
        waiting_for = self.record.waiting_for or {}
        return cast(JsonObject, waiting_for.get("request") or {})

    @property
    def call_id(self) -> str | None:
        return cast("str | None", self.request.get("id"))

    @property
    def scopes(self) -> list[str]:
        """What "always allow" on the card would remember."""
        return [str(s) for s in cast("list[str]", self.request.get("scopes") or [])]


async def find_waiting(deps: RouterDeps, session_id: str) -> Waiting | None:
    """The chat's run suspended on an approval, if it can still resume.

    A ``waiting`` record whose checkpoint is gone (an edit rewound the chat
    past it, a stop cleared it) has nothing left to resume: it is settled as
    ``cancelled`` here, so it never blocks the chat.
    """
    store = deps.store
    record = await store.waiting_run(session_id)
    if record is None:
        return None
    snapshot = None
    if store.checkpointer is not None and record.waiting_for:
        run_id = await store.get_active_run_id(session_id)
        if run_id and await store.get_active_record_id(session_id, run_id) == record.id:
            snapshot = await store.checkpointer.load(run_id)
    if snapshot is not None and snapshot.status in RESUMABLE:
        return Waiting(record, snapshot)
    await store.finish_run(
        record.id,
        status="cancelled",
        error="its checkpoint is gone",
        usage=record.usage,
    )
    log.info(
        "waiting run %s of session %s had no checkpoint: settled", record.id, session_id
    )
    return None


async def resume_waiting(
    deps: RouterDeps,
    waiting: Waiting,
    *,
    answer: Approval | None = None,
    message: InjectedContent | None = None,
    request: Request | None = None,
) -> RunController | None:
    """Resume ``waiting`` now, with no subscriber (a page attaches with
    reconnect): ``answer`` settles its card, ``message`` joins what it reads
    next. None when another request took the run first (it is live then, or
    stopped)."""
    store, sid = deps.store, waiting.session_id
    await _wound_down(deps, sid)
    claimed = await store.claim_waiting(waiting.record.id)
    if claimed is None:
        return None
    try:
        # The run's entry agent, by the registry key its record keeps: the
        # snapshot names the agent that was acting, a handoff target perhaps,
        # which the resume finds again from the entry agent's handoffs.
        name = waiting.record.agent or waiting.snapshot.agent_name
        agent = deps.agents.get(name)
        if agent is None:
            raise WebError(
                409,
                "agent_unregistered",
                f"agent {name!r} is no longer served",
                hint="serve it again to resume this run, or stop the run",
            )
        binding = deps.chat_binding(agent, await store.get(sid))
        client = await store.get_active_context(sid) or {}
        run_deps = await build_run_deps(
            deps.run_deps,
            RunTrigger(
                session_id=sid,
                agent=deps.at_root(agent, binding),
                source="user",
                request=request,
                client_context=client,
                resumed=True,
            ),
        )
        inbox = [entry.content for entry in _queued(claimed)]
        if message is not None:
            inbox.append(message)
        answers = {waiting.call_id: answer} if answer and waiting.call_id else {}
        return await deps.supervisor.start_resume(
            session_id=sid,
            agent=agent,
            snapshot=waiting.snapshot,
            client=client,
            run_deps=run_deps,
            answers=answers,
            inbox=inbox,
            autostart=True,
        )
    except BaseException:
        # Nothing resumed: the run waits on, as it was.
        await store.unclaim_waiting(waiting.record.id, claimed)
        raise


async def stop_waiting(deps: RouterDeps, waiting: Waiting) -> bool:
    """Stop a suspended run the way a stop ends a live one: the chat keeps
    what it showed, its card's call is recorded as not run, and the
    checkpoint goes. False when another request took the run first."""
    store, session, sid = deps.store, deps.session, waiting.session_id
    await _wound_down(deps, sid)
    claimed = await store.claim_waiting(waiting.record.id)
    if claimed is None:
        return False
    snapshot = waiting.snapshot
    entries = drop_system_entries(list(snapshot.entries))
    entries += [
        ToolResultEntry(call_id=call.id, output=_STOPPED, is_error=True)
        for call in pending_tool_calls(entries)
    ]
    # What it had not read yet stays in the chat, as a report that arrives
    # after a stop does: read with the user's next message.
    entries += _queued(claimed)
    meta: JsonObject = {}
    if snapshot.started_at is not None:
        meta[TIMING_META_KEY] = {
            "started_at": snapshot.started_at,
            "finished_at": time.time(),
        }
    if snapshot.context_state:
        meta[STATE_META_KEY] = snapshot.context_state
    if session is not None and entries:
        await session.append(
            sid,
            normalize_replayed_entries(entries),
            run_id=snapshot.run_id,
            meta=meta or None,
        )
    if store.checkpointer is not None:
        await store.checkpointer.delete(snapshot.run_id)
        await store.clear_active_run_id(sid, expected=snapshot.run_id)
    await store.finish_run(
        waiting.record.id,
        status="cancelled",
        error="cancelled",
        usage=waiting.record.usage,
    )
    log.info("run stop requested: session=%s (a stop while it waited on approval)", sid)
    deps.emit(
        "run_finished",
        RunFinishedData(
            session_id=sid,
            run_id=waiting.record.id,
            status="cancelled",
            error="cancelled",
            source=waiting.record.source,
        ),
    )
    return True


def _queued(waiting_for: JsonObject) -> list[InputEntry]:
    """The messages a waiting run had not read yet, oldest first."""
    queued = cast("list[dict[str, Any]]", waiting_for.get("queued") or [])
    return [cast(InputEntry, entry_from_dict(e)) for e in queued]


async def _wound_down(deps: RouterDeps, session_id: str) -> None:
    """Let the run that just suspended leave the registry. Its record reads
    ``waiting`` a moment before it does (so no fresh start slips in), and a
    resume can't register beside it — nor may a stop race its last writes."""
    live = deps.supervisor.get(session_id)
    if live is not None and live.task is not None and not live.task.done():
        await asyncio.wait([live.task])
