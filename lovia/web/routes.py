"""HTTP + SSE routes for the lovia web layer."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import FileResponse
    from sse_starlette.sse import EventSourceResponse
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from .. import events
from ..agent import Agent
from ..context_policy import ContextPolicy
from ..items import items_to_chat_messages
from ..runner import Runner
from .approvals import ApprovalRegistry
from .schemas import (
    AgentInfo,
    ApprovalRequest,
    AuditEntry,
    ChatRequest,
    ChatResponse,
    ChatSessionInfo,
    MessageOut,
    RenameRequest,
    SessionDetail,
)
from .sse import _coerce, event_to_sse
from .store import ChatStore
from .titles import generate_title

log = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"


def build_router(
    agents: dict[str, Agent[Any]],
    store: ChatStore,
    approvals: ApprovalRegistry,
    *,
    context_policy: ContextPolicy | None = None,
    audit_stream: Any | None = None,
    title_model: Any = None,
    generate_titles: bool = True,
) -> APIRouter:
    router = APIRouter()
    session = store.session

    def _pick(name: str | None) -> Agent[Any]:
        if name is None:
            if len(agents) == 1:
                return next(iter(agents.values()))
            raise HTTPException(
                status_code=400,
                detail=f"agent must be specified; available: {list(agents)}",
            )
        if name not in agents:
            raise HTTPException(status_code=404, detail=f"unknown agent {name!r}")
        return agents[name]

    async def _schedule_title(
        session_id: str, user_msg: str, output: Any, agent_name: str
    ) -> None:
        """Generate a title in the background; never propagate failures."""
        model = title_model or agents[agent_name].model
        try:
            title = await generate_title(user_msg, output, model=model)
            await store.set_title(session_id, title)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("title generation for %s failed: %s", session_id, exc)

    # ---- static UI ------------------------------------------------------

    @router.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---- agents ---------------------------------------------------------

    @router.get("/api/agents", response_model=list[AgentInfo])
    async def list_agents() -> list[AgentInfo]:
        return [
            AgentInfo(
                name=name,
                instructions=agent.instructions
                if isinstance(agent.instructions, str)
                else None,
                tools=[t.name for t in (agent.tools or [])],
            )
            for name, agent in agents.items()
        ]

    # ---- chat -----------------------------------------------------------

    @router.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        agent = _pick(req.agent)
        sid = req.session_id or uuid.uuid4().hex
        is_new = (await store.get(sid)) is None
        await store.upsert(sid, agent=agent.name)
        result = await Runner.run(
            agent,
            req.message,
            session=session,
            session_id=sid,
            context_policy=context_policy,
        )
        if is_new and generate_titles:
            asyncio.create_task(
                _schedule_title(sid, req.message, result.output, agent.name)
            )
        return ChatResponse(
            output=_coerce(result.output),
            session_id=sid,
            usage={
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        )

    @router.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest, request: Request) -> EventSourceResponse:
        agent = _pick(req.agent)
        sid = req.session_id or uuid.uuid4().hex
        is_new = (await store.get(sid)) is None
        await store.upsert(sid, agent=agent.name)

        async def gen():
            handle = Runner.stream(
                agent,
                req.message,
                session=session,
                session_id=sid,
                context_policy=context_policy,
            )
            # Tell the client its session id up front so reconnects work.
            yield {"event": "session", "data": json.dumps({"session_id": sid})}
            final_output: Any = None
            try:
                async for ev in handle:
                    if await request.is_disconnected():
                        break
                    payload = event_to_sse(ev)
                    if payload is not None:
                        yield payload
                    if isinstance(ev, events.RunCompleted):
                        final_output = ev.result.output
                    if isinstance(ev, events.ApprovalRequired):
                        await approvals.await_decision(sid, ev)
            finally:
                await approvals.release(sid)

            if is_new and generate_titles:
                # Schedule title generation as a background task so it is not
                # affected by SSE connection cancellation (e.g. the client
                # navigates away while the LLM is still thinking).  The client
                # picks up the stored title via the delayed loadSessions() poll.
                asyncio.create_task(
                    _schedule_title(sid, req.message, final_output, agent.name)
                )

        return EventSourceResponse(gen())

    @router.post("/api/chat/approve")
    async def approve(req: ApprovalRequest) -> dict[str, bool]:
        ok = await approvals.resolve(
            req.session_id, req.call_id, req.decision == "approve"
        )
        if not ok:
            raise HTTPException(status_code=404, detail="no pending approval matches")
        return {"ok": True}

    # ---- sessions -------------------------------------------------------

    @router.get("/api/sessions", response_model=list[ChatSessionInfo])
    async def list_sessions() -> list[ChatSessionInfo]:
        return [ChatSessionInfo(**m.to_dict()) for m in await store.list()]

    @router.get("/api/sessions/{session_id}", response_model=SessionDetail)
    async def get_session(session_id: str) -> SessionDetail:
        meta = await store.get(session_id)
        items = await session.load(session_id)
        msgs = items_to_chat_messages(items)
        body = [
            MessageOut(
                role=m.role,
                content=m.text or m.content,
                tool_call_id=m.tool_call_id,
                name=m.name,
                tool_calls=[
                    {
                        "id": c.id,
                        "name": c.name,
                        "arguments": c.arguments,
                    }
                    for c in m.tool_calls
                ],
            )
            for m in msgs
        ]
        if meta is None:
            from time import time as _now

            return SessionDetail(
                id=session_id,
                title=None,
                agent=None,
                created_at=_now(),
                updated_at=_now(),
                items=body,
            )
        return SessionDetail(**meta.to_dict(), items=body)

    @router.patch("/api/sessions/{session_id}", response_model=ChatSessionInfo)
    async def rename_session(session_id: str, req: RenameRequest) -> ChatSessionInfo:
        meta = await store.get(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        await store.set_title(session_id, req.title)
        meta = await store.get(session_id)
        assert meta is not None  # just updated
        return ChatSessionInfo(**meta.to_dict())

    @router.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        await store.delete(session_id)
        return {"ok": True}

    # ---- audit ----------------------------------------------------------

    @router.get("/api/sessions/{session_id}/audit", response_model=list[AuditEntry])
    async def get_audit(session_id: str) -> list[AuditEntry]:
        if audit_stream is None:
            return []
        return [
            AuditEntry(
                timestamp=r.timestamp,
                agent_name=r.agent_name,
                tool_name=r.tool_name,
                command=r.command,
                verdict=r.verdict,
                reason=r.reason,
            )
            for r in audit_stream.history()
            if r.session_id == session_id
        ]

    return router
