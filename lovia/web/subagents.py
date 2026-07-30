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
  input (recorded under source ``subagent:<id>``), so the model reacts to the
  report and the exchange persists in the transcript;
* the concurrency cap (or a lost start race) → retry with backoff, then drop
  with a warning.

This is explicit wiring, not a ``create_app`` default, because it changes
lifecycle semantics — detached children keep running after their spawning
run ends (and after a user stop; the model can still ``cancel_subagent``
them). Web defaults stay aligned with core::

    app = create_app(agent, db_path="lovia.db")
    wire_subagents(app)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

try:
    from fastapi import FastAPI, HTTPException
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..plugins.subagents import DeliverFn, SubagentReport, Subagents

if TYPE_CHECKING:
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
                    source=f"subagent:{report.id}",
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


def wire_subagents(app: FastAPI) -> int:
    """Switch served ``Subagents`` plugins to web delivery; returns how many.

    Scans every served agent for :class:`~lovia.plugins.Subagents` plugins
    still on the core default (``deliver=None``) and sets their ``deliver``
    to :func:`subagent_deliver`. Call it once, after ``create_app``. Plugins
    with a ``deliver`` of their own are left untouched.
    """
    deps: RouterDeps = app.state.deps
    fn = subagent_deliver(deps)
    wired: set[int] = set()
    for agent in deps.agents.values():
        for plugin in agent.plugins:
            if (
                isinstance(plugin, Subagents)
                and plugin.deliver is None
                and id(plugin) not in wired
            ):
                plugin.deliver = fn
                wired.add(id(plugin))
    return len(wired)


__all__ = ["subagent_deliver", "wire_subagents"]
