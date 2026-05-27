"""HTTP + SSE routes for the lovia web layer."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from .. import events
from ..agent import Agent
from ..runner import Runner
from ..session import Session
from .approvals import ApprovalRegistry
from .schemas import AgentInfo, ApprovalRequest, ChatRequest, ChatResponse, MessageOut
from .sse import _coerce, event_to_sse

_STATIC = Path(__file__).parent / "static"


def build_router(
    agents: dict[str, Agent[Any, Any]],
    session: Session,
    approvals: ApprovalRegistry,
) -> APIRouter:
    router = APIRouter()

    def _pick(name: str | None) -> Agent[Any, Any]:
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

    @router.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

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

    @router.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        agent = _pick(req.agent)
        sid = req.session_id or uuid.uuid4().hex
        result = await Runner.run(agent, req.message, session=session, session_id=sid)
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

        async def gen():
            handle = Runner.run_streamed(
                agent, req.message, session=session, session_id=sid
            )
            # Tell the client its session id up front so reconnects work.
            yield {"event": "session", "data": json.dumps({"session_id": sid})}
            try:
                async for ev in handle:
                    if await request.is_disconnected():
                        break
                    payload = event_to_sse(ev)
                    if payload is not None:
                        yield payload
                    # ApprovalRequired pauses the runner via the registry; the
                    # client posts to /api/chat/approve to unblock it.
                    if isinstance(ev, events.ApprovalRequired):
                        await approvals.await_decision(sid, ev)
            finally:
                # Default-deny anything still pending so the runner unblocks
                # even if the client disconnected mid-decision.
                await approvals.release(sid)

        return EventSourceResponse(gen())

    @router.post("/api/chat/approve")
    async def approve(req: ApprovalRequest) -> dict[str, bool]:
        ok = await approvals.resolve(
            req.session_id, req.call_id, req.decision == "approve"
        )
        if not ok:
            raise HTTPException(status_code=404, detail="no pending approval matches")
        return {"ok": True}

    @router.get("/api/sessions/{session_id}", response_model=list[MessageOut])
    async def get_session(session_id: str) -> list[MessageOut]:
        msgs = await session.load(session_id)
        return [
            MessageOut(
                role=m.role,
                content=m.text or m.content,
                tool_call_id=m.tool_call_id,
                name=m.name,
            )
            for m in msgs
        ]

    @router.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        await session.clear(session_id)
        return {"ok": True}

    return router
