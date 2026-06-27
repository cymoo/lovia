"""Schedule routes: list / create / delete / pause-resume scheduled runs.

The scheduler loop itself lives in :mod:`lovia.web.scheduler` and is started by
``create_app``'s lifespan; these endpoints only persist the schedule rows it
polls.
"""

from __future__ import annotations

import time
import uuid

try:
    from fastapi import APIRouter, HTTPException
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..scheduler import initial_next_fire, validate_trigger
from ..schemas import ScheduleInfo, SchedulePatch, ScheduleSpec
from ..store import ScheduleRow
from .deps import RouterDeps


def _info(row: ScheduleRow) -> ScheduleInfo:
    return ScheduleInfo(
        id=row.id,
        agent=row.agent,
        input=row.input,
        session_id=row.session_id,
        trigger_kind=row.trigger_kind,
        trigger_expr=row.trigger_expr,
        next_fire=row.next_fire,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def build_schedules_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()
    store = deps.store

    @router.get("/api/schedules", response_model=list[ScheduleInfo])
    async def list_schedules() -> list[ScheduleInfo]:
        return [_info(r) for r in await store.list_schedules()]

    @router.post("/api/schedules", response_model=ScheduleInfo)
    async def create_schedule(spec: ScheduleSpec) -> ScheduleInfo:
        message = spec.input.strip()
        if not message:
            raise HTTPException(status_code=422, detail="empty input")
        agent_name = spec.agent or deps.default_agent
        if agent_name is None or agent_name not in deps.agents:
            raise HTTPException(status_code=404, detail=f"unknown agent {spec.agent!r}")
        try:
            validate_trigger(spec.trigger_kind, spec.trigger_expr)
            next_fire = initial_next_fire(
                spec.trigger_kind, spec.trigger_expr, now=time.time()
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
        if patch.active and not row.active:
            # Resume: recompute next_fire from now so it doesn't fire stale slots.
            try:
                nxt = initial_next_fire(
                    row.trigger_kind, row.trigger_expr, now=time.time()
                )
            except (ValueError, RuntimeError):
                nxt = row.next_fire
            await store.set_schedule_active(schedule_id, active=True, next_fire=nxt)
        elif not patch.active and row.active:
            await store.set_schedule_active(schedule_id, active=False)
        return _info((await store.get_schedule(schedule_id)) or row)

    return router
