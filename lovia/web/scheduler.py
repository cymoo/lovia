"""Scheduled / deferred background runs (Phase 3).

A single async loop polls the ``schedules`` table and, when a schedule is due,
fires a **supervised** run via :class:`~lovia.web.supervisor.RunSupervisor`
(headless — no client needs to be attached). Triggers: ``cron`` (croniter),
``every`` (interval seconds), ``at`` (one-shot epoch timestamp).

Delivery is **at-most-once, coalesced**: an overdue schedule fires once
(missed slots collapse), ``next_fire`` is advanced *before* the run starts (a
crash mid-fire won't re-fire the slot), and a fire is skipped if that
schedule's previous run is still live. Restart recovery is automatic — the
loop simply reads the durable store on boot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import TYPE_CHECKING

try:
    from fastapi import HTTPException
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from .store import RunRow
from .titles import provisional_title

if TYPE_CHECKING:
    from .api.deps import RouterDeps
    from .store import ScheduleRow

log = logging.getLogger(__name__)

TRIGGER_KINDS = ("cron", "every", "at")


# --------------------------------------------------------------------------- #
# Trigger math
# --------------------------------------------------------------------------- #


def _interval_seconds(expr: str) -> float:
    secs = float(expr)
    if secs <= 0:
        raise ValueError("an 'every' interval must be > 0 seconds")
    return secs


def _croniter_next(expr: str, after: float) -> float:
    try:
        from croniter import croniter
    except ImportError as exc:  # pragma: no cover - cron is opt-in
        raise RuntimeError(
            "cron triggers need the 'croniter' package — install lovia[web]"
        ) from exc
    return float(croniter(expr, after).get_next(float))


def validate_trigger(kind: str, expr: str) -> None:
    """Raise ``ValueError`` (→ 422) if ``(kind, expr)`` is not a valid trigger."""
    if kind == "at":
        float(expr)  # an epoch timestamp
    elif kind == "every":
        _interval_seconds(expr)
    elif kind == "cron":
        try:
            _croniter_next(expr, time.time())  # croniter validates the expression
        except RuntimeError:
            raise  # croniter not installed — surface as-is
        except Exception as exc:
            raise ValueError(f"invalid cron expression {expr!r}: {exc}") from exc
    else:
        raise ValueError(f"unknown trigger kind {kind!r} (use one of {TRIGGER_KINDS})")


def initial_next_fire(kind: str, expr: str, *, now: float) -> float:
    """The first ``next_fire`` when a schedule is created."""
    if kind == "at":
        return float(expr)
    if kind == "every":
        return now + _interval_seconds(expr)
    if kind == "cron":
        return _croniter_next(expr, now)
    raise ValueError(f"unknown trigger kind {kind!r}")


def advance_next_fire(kind: str, expr: str, *, now: float) -> float | None:
    """The next ``next_fire`` after a fire, or ``None`` for a one-shot (``at``)
    that should deactivate. Coalesced: always the first slot strictly after
    ``now`` (missed slots are skipped)."""
    if kind == "at":
        return None
    if kind == "every":
        return now + _interval_seconds(expr)
    if kind == "cron":
        return _croniter_next(expr, now)
    raise ValueError(f"unknown trigger kind {kind!r}")


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #


class Scheduler:
    def __init__(self, deps: RouterDeps, *, poll_interval: float = 1.0) -> None:
        self.deps = deps
        self.store = deps.store
        self._poll = poll_interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("scheduler tick failed: %s", exc)
            await asyncio.sleep(self._poll)

    async def run_due(self) -> None:
        """Fire every schedule that is due. Public so tests can drive it."""
        now = time.time()
        for sched in await self.store.due_schedules(now):
            await self._fire(sched, now)

    async def fire_now(self, sched: ScheduleRow) -> str | None:
        """Fire ``sched`` immediately (``POST /api/schedules/{id}/run``).

        Counts as a regular fire: the cadence advances (an interval restarts
        from now, a one-shot completes) but a paused schedule stays paused.
        Returns the target session id, or ``None`` when the fire was skipped.
        """
        return await self._fire(sched, time.time())

    async def _fire(self, sched: ScheduleRow, now: float) -> str | None:
        """Fire one schedule; returns the session it ran/injected into, or
        ``None`` when skipped (agent unavailable, previous run still live, or
        deferred at the concurrency cap)."""
        agent_name = sched.agent or self.deps.default_agent
        if agent_name is not None and agent_name not in self.deps.agents:
            # The schedule_run tool stores ``agent.name``, which may differ from
            # the registry key (create_app({"alias": agent})) — recover by
            # matching the self-name before declaring the agent gone.
            agent_name = next(
                (k for k, a in self.deps.agents.items() if a.name == agent_name),
                agent_name,
            )
        if agent_name is None or agent_name not in self.deps.agents:
            detail = (
                f"agent {sched.agent!r} is not served anymore"
                if sched.agent
                else "no agent specified and no default is available"
            )
            log.warning("schedule %s: %s; advancing", sched.id, detail)
            await self._advance(sched, now, last_session_id=sched.last_session_id)
            # No run ever started, so the supervisor writes no record — file a
            # zero-length failed one so the schedule's history shows the miss.
            # Best-effort bookkeeping: never let it break the fire loop.
            try:
                await self.store.start_run(
                    RunRow(
                        id=uuid.uuid4().hex,
                        session_id=None,
                        agent=agent_name,
                        source=f"schedule:{sched.id}",
                        status="failed",
                        error=detail,
                        started_at=now,
                        finished_at=now,
                        usage=None,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("schedule %s: run record write failed: %s", sched.id, exc)
            return None
        agent = self.deps.agents[agent_name]

        if sched.session_id is not None:
            # Continue a fixed conversation: inject if a run is live, else start.
            target = sched.session_id
            live = self.deps.supervisor.get(target)
            if live is not None:
                live.inject(sched.input)
                await self._advance(sched, now, last_session_id=target)
                # Delivered into a live run — that run's outcome is its own
                # (and recorded under the run's own source), so no record here.
                return target
            is_new = (await self.store.get(target)) is None
        else:
            # Fresh session per fire — but skip if the previous fire's run is
            # still going (don't pile up).
            prev = sched.last_session_id
            if prev is not None and self.deps.supervisor.get(prev) is not None:
                log.info("schedule %s: previous run still active; skipping", sched.id)
                await self._advance(sched, now, last_session_id=prev)
                return None
            target = uuid.uuid4().hex
            is_new = True

        try:
            title = provisional_title(sched.input) if is_new else None
            await self.store.upsert(
                target,
                agent=agent_name,  # registry key — the identity pick() speaks
                title=title,
            )
            await self.deps.supervisor.start(
                session_id=target,
                agent=agent,
                input=sched.input,
                is_new=is_new,
                title_message=sched.input,
                autostart=True,  # clientless: begin the run with no subscriber
                # The supervisor records the run under this source, which is
                # exactly what makes it this schedule's history/last outcome.
                source=f"schedule:{sched.id}",
            )
        except HTTPException as exc:
            if exc.status_code == 429:
                # At the concurrency cap: defer (leave next_fire due → retried).
                # Drop the freshly-created session row so repeated 429s on a
                # fresh-session schedule don't leak empty chats (each retry mints
                # a new UUID). start() raises 429 before any state change, so the
                # only side effect to undo is our own upsert above.
                log.info("schedule %s: at concurrency cap; deferring", sched.id)
                if is_new:
                    await self.store.delete(target)
                return None
            raise
        if is_new:
            # After start() succeeded — a 429-deferred fire deletes its fresh
            # session above, which must not have been announced.
            self.deps.emit(
                "session_created", session_id=target, agent=agent_name, title=title
            )
        await self._advance(sched, now, last_session_id=target)
        return target

    async def _advance(
        self, sched: ScheduleRow, now: float, *, last_session_id: str | None
    ) -> None:
        nxt = advance_next_fire(sched.trigger_kind, sched.trigger_expr, now=now)
        await self.store.mark_fired(
            sched.id,
            next_fire=nxt if nxt is not None else now,  # one-shot: unused once inactive
            # ``and sched.active`` keeps a manually-fired paused schedule paused
            # (the polling loop only ever fires active rows, where it's a no-op).
            active=nxt is not None and sched.active,
            last_session_id=last_session_id,
        )
