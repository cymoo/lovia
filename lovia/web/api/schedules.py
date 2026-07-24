"""Schedule routes: list / fetch / create / edit / delete / run-now.

The scheduler loop itself lives in :mod:`lovia.web.scheduler` and is started by
``create_app``'s lifespan; these endpoints persist the schedule rows it polls
(plus a run-now that fires through the same machinery, loop not required).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Query
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..scheduler import Scheduler, initial_next_fire, validate_trigger
from ..schemas import RunRecordInfo, ScheduleInfo, SchedulePatch, ScheduleSpec
from ..store import RunRow, ScheduleRow
from .deps import RouterDeps
from .serialization import run_record


def _info(row: ScheduleRow, last: RunRow | None = None) -> ScheduleInfo:
    """Project a schedule row (+ its latest run record) onto the wire shape.

    ``last_status``/``last_error`` are derived, not stored: the run records
    keyed by ``source = "schedule:<id>"`` are the single source of truth for
    outcomes. A still-``running`` record reads as "no outcome yet" (None),
    matching a schedule that has never fired.
    """
    last_status = last_error = None
    if last is not None and last.status != "running":
        last_status = "ok" if last.status == "completed" else "error"
        last_error = last.error
    return ScheduleInfo(
        id=row.id,
        agent=row.agent,
        input=row.input,
        session_id=row.session_id,
        trigger_kind=row.trigger_kind,
        trigger_expr=row.trigger_expr,
        next_fire=row.next_fire,
        active=row.active,
        last_session_id=row.last_session_id,
        last_status=last_status,
        last_error=last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        until=row.until,
        max_fires=row.max_fires,
        expires_at=row.expires_at,
        fire_count=row.fire_count,
        finished_reason=row.finished_reason,
    )


def _resolve_agent_name(deps: RouterDeps, name: str | None) -> str:
    """Validate an explicit agent name, or fall back to the server default."""
    agent_name = name or deps.default_agent
    if agent_name is None or agent_name not in deps.agents:
        # Distinguish "named an unknown agent" from "named none and there's no
        # default" (multi-agent server) — the latter reported a useless `None`.
        detail = (
            f"unknown agent {agent_name!r}"
            if agent_name is not None
            else "no agent specified and no default is available"
        )
        raise HTTPException(status_code=404, detail=detail)
    return agent_name


def _validated_next_fire(kind: str, expr: str) -> float:
    """Validate a trigger and compute its first fire; ValueError → 422."""
    try:
        validate_trigger(kind, expr)
        return initial_next_fire(kind, expr, now=time.time())
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def build_schedules_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()
    store = deps.store
    # Fire-now shares the polling loop's machinery (inject/skip/defer rules)
    # without needing that loop to run — this instance is never start()ed.
    fire_scheduler = Scheduler(deps)

    async def _with_last(row: ScheduleRow) -> ScheduleInfo:
        return _info(row, await store.latest_run_for(f"schedule:{row.id}"))

    @router.get("/api/schedules", response_model=list[ScheduleInfo])
    async def list_schedules() -> list[ScheduleInfo]:
        return [await _with_last(r) for r in await store.list_schedules()]

    @router.get("/api/schedules/{schedule_id}", response_model=ScheduleInfo)
    async def get_schedule(schedule_id: str) -> ScheduleInfo:
        row = await store.get_schedule(schedule_id)
        if row is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        return await _with_last(row)

    @router.get("/api/schedules/{schedule_id}/runs", response_model=list[RunRecordInfo])
    async def schedule_runs(
        schedule_id: str, limit: int = Query(20, ge=1, le=200)
    ) -> list[RunRecordInfo]:
        """This schedule's fire history, newest first (its run records)."""
        if await store.get_schedule(schedule_id) is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        rows = await store.list_runs(source=f"schedule:{schedule_id}", limit=limit)
        return [run_record(r) for r in rows]

    @router.post("/api/schedules", response_model=ScheduleInfo)
    async def create_schedule(spec: ScheduleSpec) -> ScheduleInfo:
        message = spec.input.strip()
        if not message:
            raise HTTPException(status_code=422, detail="empty input")
        agent_name = _resolve_agent_name(deps, spec.agent)
        next_fire = _validated_next_fire(spec.trigger_kind, spec.trigger_expr)

        now = time.time()
        row = ScheduleRow(
            id=uuid.uuid4().hex,
            agent=agent_name,
            input=message,
            session_id=spec.session_id,
            trigger_kind=spec.trigger_kind,
            trigger_expr=spec.trigger_expr,
            next_fire=next_fire,
            active=True,
            last_session_id=None,
            created_at=now,
            updated_at=now,
            until=spec.until.strip() or None if spec.until else None,
            max_fires=spec.max_fires,
            expires_at=spec.expires_at,
        )
        await store.add_schedule(row)
        return _info(row)

    @router.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> dict[str, bool]:
        if not await store.delete_schedule(schedule_id):
            raise HTTPException(status_code=404, detail="schedule not found")
        return {"ok": True}

    @router.patch("/api/schedules/{schedule_id}", response_model=ScheduleInfo)
    async def patch_schedule(schedule_id: str, patch: SchedulePatch) -> ScheduleInfo:
        row = await store.get_schedule(schedule_id)
        if row is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        provided = patch.model_fields_set

        changes: dict[str, Any] = {}
        if patch.input is not None:
            message = patch.input.strip()
            if not message:
                raise HTTPException(status_code=422, detail="empty input")
            changes["input"] = message
        if patch.agent is not None:
            changes["agent"] = _resolve_agent_name(deps, patch.agent)
        if "session_id" in provided:
            # Explicit null detaches (fresh session per fire); omitted keeps.
            changes["session_id"] = patch.session_id
        if "until" in provided:
            changes["until"] = patch.until.strip() or None if patch.until else None
        if "max_fires" in provided:
            changes["max_fires"] = patch.max_fires
        if "expires_at" in provided:
            changes["expires_at"] = patch.expires_at

        kind = patch.trigger_kind or row.trigger_kind
        expr = (
            patch.trigger_expr if patch.trigger_expr is not None else row.trigger_expr
        )
        trigger_changed = kind != row.trigger_kind or expr != row.trigger_expr
        resuming = patch.active is True and not row.active
        if trigger_changed:
            changes.update(trigger_kind=kind, trigger_expr=expr)
        if trigger_changed or resuming:
            # A new trigger starts from now; a resume also recomputes so the
            # schedule doesn't immediately fire slots that lapsed while paused.
            changes["next_fire"] = _validated_next_fire(kind, expr)
        if resuming:
            # Live again — shed the "done" marker (condition met / expired).
            changes["finished_reason"] = None
        if patch.active is not None:
            changes["active"] = patch.active

        if changes:
            row = replace(row, updated_at=time.time(), **changes)
            await store.update_schedule(row)
        return await _with_last(row)

    @router.post("/api/schedules/{schedule_id}/run")
    async def run_schedule(schedule_id: str) -> dict[str, Any]:
        """Fire a schedule immediately (advancing its cadence; paused stays
        paused). 409 when skipped — previous run still live or at capacity."""
        row = await store.get_schedule(schedule_id)
        if row is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        target = await fire_scheduler.fire_now(row)
        if target is None:
            raise HTTPException(
                status_code=409,
                detail="not fired: previous run still active, agent unavailable, "
                "or at the concurrency cap",
            )
        return {"ok": True, "session_id": target}

    return router
