"""Schedule routes: list / fetch / create / edit / delete / run-now / preview.

The scheduler loop itself lives in :mod:`lovia.web.scheduler` and is started by
``create_app``'s lifespan; these endpoints persist the schedule rows it polls
(plus a run-now that fires through the same machinery, loop not required).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

try:
    from fastapi import APIRouter, Query
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..errors import WebError
from ..scheduler import (
    Scheduler,
    advance_next_fire,
    initial_next_fire,
    validate_trigger,
)
from ..schemas import (
    RunRecordInfo,
    ScheduleInfo,
    SchedulePatch,
    SchedulePreview,
    SchedulePreviewRequest,
    ScheduleSpec,
)
from ..store import RunRow, ScheduleRow
from .deps import RouterDeps
from .serialization import run_record

_PREVIEW_FIRES = 3


def _info(
    row: ScheduleRow, last: RunRow | None = None, session_title: str | None = None
) -> ScheduleInfo:
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
        name=row.name,
        agent=row.agent,
        input=row.input,
        session_id=row.session_id,
        session_title=session_title,
        workspace_root=row.workspace_root,
        workspace_mode=row.workspace_mode,
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
    if agent_name is None:
        # Multi-agent server, none named — distinct from an unknown name, which
        # used to report a useless `None`.
        raise WebError(
            400,
            "invalid_request",
            "no agent specified and no default is available",
            hint=f"available: {list(deps.agents)}",
        )
    if agent_name not in deps.agents:
        raise WebError(404, "agent_not_found", f"unknown agent {agent_name!r}")
    return agent_name


def _validated_next_fire(kind: str, expr: str) -> float:
    """Validate a trigger and compute its first fire; ValueError → 422."""
    try:
        validate_trigger(kind, expr)
        return initial_next_fire(kind, expr, now=time.time())
    except (ValueError, RuntimeError) as exc:
        raise WebError.from_exc(422, "invalid_request", exc) from exc


def _name(value: str | None) -> str | None:
    return (value.strip() or None) if value else None


def build_schedules_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()
    store = deps.store
    # Fire-now shares the polling loop's machinery (inject/skip/defer rules)
    # without needing that loop to run — this instance is never start()ed.
    fire_scheduler = Scheduler(deps)

    async def _with_last(row: ScheduleRow) -> ScheduleInfo:
        chat = await store.get(row.session_id) if row.session_id else None
        return _info(
            row,
            await store.latest_run_for(f"schedule:{row.id}"),
            chat.title if chat else None,
        )

    def _binding(
        agent_name: str, mode: str | None, root: str | None
    ) -> tuple[str | None, str | None]:
        """The ``(workspace_root, workspace_mode)`` to store, checked as for a
        new chat. A root is stored only when one was asked for, so a schedule
        without one follows its agent's directory."""
        # schedule_run stores the agent's own name, which may not be its key.
        agent = deps.agents.get(agent_name) or next(
            (a for a in deps.agents.values() if a.name == agent_name), None
        )
        if agent is None:
            raise WebError(404, "agent_not_found", f"unknown agent {agent_name!r}")
        binding = deps.requested_binding(agent, mode, root)
        return (binding.root if root is not None else None), binding.mode

    @router.get("/api/schedules", response_model=list[ScheduleInfo])
    async def list_schedules() -> list[ScheduleInfo]:
        return [await _with_last(r) for r in await store.list_schedules()]

    @router.get("/api/schedules/{schedule_id}", response_model=ScheduleInfo)
    async def get_schedule(schedule_id: str) -> ScheduleInfo:
        row = await store.get_schedule(schedule_id)
        if row is None:
            raise WebError(404, "schedule_not_found", "schedule not found")
        return await _with_last(row)

    @router.get("/api/schedules/{schedule_id}/runs", response_model=list[RunRecordInfo])
    async def schedule_runs(
        schedule_id: str, limit: int = Query(20, ge=1, le=200)
    ) -> list[RunRecordInfo]:
        """This schedule's fire history, newest first (its run records)."""
        if await store.get_schedule(schedule_id) is None:
            raise WebError(404, "schedule_not_found", "schedule not found")
        rows = await store.list_runs(source=f"schedule:{schedule_id}", limit=limit)
        return [run_record(r) for r in rows]

    @router.post("/api/schedules", response_model=ScheduleInfo)
    async def create_schedule(spec: ScheduleSpec) -> ScheduleInfo:
        message = spec.input.strip()
        if not message:
            raise WebError(422, "invalid_request", "empty input")
        asked = spec.agent
        if asked is None and spec.session_id:
            # Its fires continue that chat: by default, as the agent it has.
            chat = await store.get(spec.session_id)
            if chat is not None and chat.agent in deps.agents:
                asked = chat.agent
        agent_name = _resolve_agent_name(deps, asked)
        next_fire = _validated_next_fire(spec.trigger_kind, spec.trigger_expr)
        switch = deps.workspace_switch
        root, mode = _binding(
            agent_name,
            spec.workspace_mode or (switch.default_mode if switch else None),
            spec.workspace_root,
        )

        now = time.time()
        row = ScheduleRow(
            id=uuid.uuid4().hex,
            name=_name(spec.name),
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
            until=(spec.until.strip() or None) if spec.until else None,
            max_fires=spec.max_fires,
            expires_at=spec.expires_at,
            context=spec.context,
            workspace_root=root,
            workspace_mode=mode,
        )
        await store.add_schedule(row)
        return await _with_last(row)

    @router.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> dict[str, bool]:
        if not await store.delete_schedule(schedule_id):
            raise WebError(404, "schedule_not_found", "schedule not found")
        return {"ok": True}

    @router.patch("/api/schedules/{schedule_id}", response_model=ScheduleInfo)
    async def patch_schedule(schedule_id: str, patch: SchedulePatch) -> ScheduleInfo:
        row = await store.get_schedule(schedule_id)
        if row is None:
            raise WebError(404, "schedule_not_found", "schedule not found")
        provided = patch.model_fields_set

        changes: dict[str, Any] = {}
        if patch.input is not None:
            message = patch.input.strip()
            if not message:
                raise WebError(422, "invalid_request", "empty input")
            changes["input"] = message
        if "name" in provided:
            changes["name"] = _name(patch.name)
        if patch.agent is not None:
            changes["agent"] = _resolve_agent_name(deps, patch.agent)
        if provided & {"workspace_root", "workspace_mode"}:
            # Explicit null: the agent's own directory / policy.
            changes["workspace_root"], changes["workspace_mode"] = _binding(
                changes.get("agent") or row.agent or _resolve_agent_name(deps, None),
                patch.workspace_mode
                if "workspace_mode" in provided
                else row.workspace_mode,
                patch.workspace_root
                if "workspace_root" in provided
                else row.workspace_root,
            )
        if "session_id" in provided:
            # Explicit null detaches (fresh session per fire); omitted keeps.
            changes["session_id"] = patch.session_id
        if "until" in provided:
            changes["until"] = (patch.until.strip() or None) if patch.until else None
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
            raise WebError(404, "schedule_not_found", "schedule not found")
        target = await fire_scheduler.fire_now(row)
        if target is None:
            raise WebError(
                409,
                "schedule_not_fired",
                "not fired: previous run still active, agent unavailable, "
                "or at the concurrency cap",
            )
        return {"ok": True, "session_id": target}

    @router.post("/api/schedules/preview", response_model=SchedulePreview)
    async def preview_schedule(req: SchedulePreviewRequest) -> SchedulePreview:
        """The trigger's next fires were it saved now; 422 when it's invalid."""
        kind, expr = req.trigger_kind, req.trigger_expr
        fires = [_validated_next_fire(kind, expr)]
        while len(fires) < _PREVIEW_FIRES:
            nxt = advance_next_fire(kind, expr, now=fires[-1])
            if nxt is None:  # a one-shot
                break
            fires.append(nxt)
        offset = datetime.now().astimezone().utcoffset()
        return SchedulePreview(
            next_fires=fires,
            utc_offset=int(offset.total_seconds()) if offset else 0,
        )

    return router
