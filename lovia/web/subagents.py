"""Web delivery for background subagents: inject-or-start into the parent chat.

The core :class:`~lovia.plugins.Subagents` default is **bounded** — children
never outlive their run. A chat UI usually wants the detached experience
instead: the model answers now, and a subagent's report lands in the
conversation whenever it is ready. :func:`wire_subagents` flips every served
``Subagents`` plugin still on the core default (``deliver=None``) into
detached mode, with a deliver that routes each report to its parent session
the same way the scheduler routes a fire (see ``Scheduler._fire``):

* the session has a live supervised run → inject into its mailbox (the next
  turn sees it; the supervisor's auto-chain covers a run that is just
  finishing);
* no live run → start a clientless supervised run with the report as its
  input (recorded under source ``subagent-report:<task id>``, distinct from
  the child task run's ``subagent:<session>``), so the model reacts to the
  report and the exchange persists in the transcript;
* the concurrency cap (or a lost start race) → retry with backoff, then drop
  with a warning.

Attaching the plugin stays the user's explicit choice; *adapting* an
attached plugin to the serving context is automatic — ``create_app`` wires
any served ``Subagents`` still on core defaults (opt out with
``create_app(..., wire_subagents=False)``). Wired children keep running
after their spawning run ends and after a user stop of the parent (the
model can still ``cancel_subagent`` them; the task's own stop button works
too).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import TYPE_CHECKING

try:
    from fastapi import FastAPI, HTTPException
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..exceptions import RunCancelled
from ..plugins.subagents import (
    ChildSpec,
    DeliverFn,
    RunChildFn,
    SubagentReport,
    Subagents,
)

if TYPE_CHECKING:
    from ..runtime.result import RunResult
    from .api.deps import RouterDeps

log = logging.getLogger(__name__)

# Backoff between delivery attempts when the supervisor is at its concurrency
# cap (or a start race was lost). Long enough for a slot to free up, short
# enough that a report still lands while the user is around.
_RETRY_DELAYS = (0.0, 2.0, 5.0, 15.0, 30.0)


def subagent_deliver(deps: "RouterDeps") -> DeliverFn:
    """Build the inject-or-start :data:`~lovia.plugins.DeliverFn` for ``deps``."""

    async def deliver(report: SubagentReport) -> None:
        sid = report.session_id
        if sid is None:
            log.info("subagent %s: no session to deliver to; dropped", report.id)
            return
        for delay in _RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            live = deps.supervisor.get(sid)
            if live is not None:
                live.inject(report.text)
                log.info(
                    "subagent %s: report injected into session %s's live run "
                    "(visible at its next turn)",
                    report.id,
                    sid,
                )
                return
            row = await deps.store.get(sid)
            if row is None:
                log.info(
                    "subagent %s: session %s is gone; report dropped",
                    report.id,
                    sid,
                )
                return
            try:
                agent = deps.pick(row.agent)
            except HTTPException:
                log.warning(
                    "subagent %s: agent %r is not served anymore; report dropped",
                    report.id,
                    row.agent,
                )
                return
            try:
                await deps.supervisor.start(
                    session_id=sid,
                    agent=agent,
                    input=report.text,
                    is_new=False,
                    title_message=None,
                    autostart=True,  # clientless: consume the report unattended
                    source=f"subagent-report:{report.id}",
                )
                log.info(
                    "subagent %s: report delivered to idle session %s via a new run",
                    report.id,
                    sid,
                )
                return
            except HTTPException as exc:
                # 409: another run claimed the session between get() and
                # start() — the next attempt injects into it. 429: at the
                # concurrency cap — wait for a slot.
                if exc.status_code not in (409, 429):
                    raise
        log.warning(
            "subagent %s: could not deliver to session %s "
            "(supervisor busy); report dropped",
            report.id,
            sid,
        )

    return deliver


def subagent_runner(deps: "RouterDeps") -> RunChildFn:
    """Build the supervised :data:`~lovia.plugins.RunChildFn` for ``deps``.

    Each spawn becomes a **clientless supervised run in its own task
    session** (``parent_id`` = the spawning chat), so the child streams over
    the ordinary session SSE, shows up in the UI's Tasks group, can be
    stopped there, and leaves a run record + transcript behind. The child's
    outcome is read off the controller (``final_status``/``final_output``)
    once its task finishes.

    Cancellation bridges both ways: the UI's stop button cancels the
    supervised run (→ :class:`~lovia.exceptions.RunCancelled` here → no
    report), and a ``cancel_subagent`` call trips ``spec.token``, which a
    small watcher translates into ``supervisor.cancel`` — going through the
    supervisor keeps its terminal bookkeeping (partial persist, checkpoint
    drop, "cancelled" record) intact, which a raw shared token would bypass.
    """

    async def run_child(spec: ChildSpec) -> "RunResult":
        from ..runtime.result import RunResult
        from .titles import provisional_title

        child_sid = uuid.uuid4().hex
        # The registry key the task chat speaks afterwards. The child agent
        # itself is unregistered (often a clone), so record the parent chat's
        # agent — a follow-up message in the finished task then runs the
        # served agent closest to it.
        agent_key: str | None = None
        if spec.parent_session_id is not None:
            row = await deps.store.get(spec.parent_session_id)
            if row is not None:
                agent_key = row.agent
        await deps.store.upsert(
            child_sid,
            agent=agent_key,
            title=f"[{spec.id}] {provisional_title(spec.prompt)}",
            parent_id=spec.parent_session_id,
        )
        try:
            ctrl = await deps.supervisor.start(
                session_id=child_sid,
                agent=spec.agent,
                input=spec.prompt,
                is_new=False,  # no title generation for task sessions
                title_message=None,
                autostart=True,
                source=f"subagent:{child_sid}",
                # The parent's send_to_subagent pushes into this same channel,
                # joining whatever the task chat's own /inject sends.
                mailbox=spec.mailbox,
            )
        except HTTPException as exc:
            await deps.store.delete(child_sid)
            if exc.status_code == 429:
                raise RuntimeError(
                    "the server is at its concurrent-run limit; "
                    "wait for other work to finish and spawn again"
                ) from exc
            raise
        assert ctrl.task is not None  # autostart began it
        log.info(
            "subagent %s: task session %s started (agent=%s, parent=%s)",
            spec.id,
            child_sid,
            spec.agent.name,
            spec.parent_session_id,
        )
        deps.emit(
            "session_created",
            session_id=child_sid,
            agent=agent_key,
            title=f"[{spec.id}] {provisional_title(spec.prompt)}",
        )

        async def watch_token() -> None:
            while not spec.token.is_cancelled:
                await asyncio.sleep(0.3)
            deps.supervisor.cancel(child_sid)

        watcher = asyncio.create_task(watch_token())
        try:
            await ctrl.task
        finally:
            # Cancel *and* join (the Scheduler.stop() idiom) so no pending
            # watcher outlives run_child.
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        log.info(
            "subagent %s: task session %s ended: %s",
            spec.id,
            child_sid,
            ctrl.final_status,
        )
        if ctrl.final_status == "completed":
            return RunResult(
                output=ctrl.final_output,
                entries=[],
                final_agent=spec.agent,
                usage=ctrl.usage,
                turns=ctrl.turns,
            )
        if ctrl.final_status == "cancelled":
            raise RunCancelled("subagent stopped")
        raise RuntimeError(ctrl.final_error or "subagent run did not complete")

    return run_child


def _wire(deps: "RouterDeps") -> int:
    """Adapt served ``Subagents`` plugins to this app; returns how many.

    A plugin still entirely on core defaults (``deliver`` **and**
    ``run_child`` both ``None``) gets the pair set together: supervised
    children + inject-or-start delivery. A plugin with either seam already
    set is considered hand-wired and left untouched.
    """
    deliver = subagent_deliver(deps)
    run_child = subagent_runner(deps)
    wired: set[int] = set()
    for agent in deps.agents.values():
        for plugin in agent.plugins:
            if (
                isinstance(plugin, Subagents)
                and plugin.deliver is None
                and plugin.run_child is None
                and id(plugin) not in wired
            ):
                plugin.deliver = deliver
                plugin.run_child = run_child
                wired.add(id(plugin))
    return len(wired)


def wire_subagents(app: FastAPI) -> int:
    """Adapt served ``Subagents`` plugins to web semantics; returns how many.

    ``create_app`` calls this automatically (disable with
    ``create_app(..., wire_subagents=False)``); the helper exists for apps
    that mount :func:`~lovia.web.build_api_router` into their own FastAPI
    app. See :func:`subagent_runner` and :func:`subagent_deliver` for what
    the wiring does.
    """
    return _wire(app.state.deps)


__all__ = ["subagent_deliver", "subagent_runner", "wire_subagents"]
