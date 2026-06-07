"""HTTP + SSE routes for the lovia web layer."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Query, Request
    from fastapi.templating import Jinja2Templates
    from sse_starlette.sse import EventSourceResponse
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from .. import events
from ..agent import Agent
from ..context_policy import ContextPolicy
from ..reliability import CancelToken
from ..transcript import (
    AssistantTextEntry,
    ReasoningEntry,
    ToolCallEntry,
    entries_to_messages,
)
from ..runner import Runner
from .approvals import ApprovalRegistry
from .schemas import (
    AgentInfo,
    ApprovalRequest,
    ChatRequest,
    ChatResponse,
    ChatSessionInfo,
    MarkdownRequest,
    MarkdownResponse,
    MessageOut,
    RenameRequest,
    SessionDetail,
)
from .markdown import render_markdown
from .sse import _coerce, event_to_sse
from .store import ChatStore
from .titles import generate_title

log = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def build_router(
    agents: dict[str, Agent[Any]],
    store: ChatStore,
    approvals: ApprovalRegistry,
    *,
    context_policy: ContextPolicy | None = None,
    title_model: Any = None,
    generate_titles: bool = True,
    title: str = "lovia",
) -> APIRouter:
    router = APIRouter()
    session = store.session
    # Per-session CancelTokens for cooperative cancellation via the stop button.
    _cancel_tokens: dict[str, CancelToken] = {}

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
    async def index(request: Request) -> Any:
        return _TEMPLATES.TemplateResponse(request, "index.html", {"title": title})

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

    @router.post("/api/markdown", response_model=MarkdownResponse)
    async def markdown(req: MarkdownRequest) -> MarkdownResponse:
        return MarkdownResponse(html=render_markdown(req.text))

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

        # Cancel any previous run on the same session so the stop button
        # only affects the current stream.
        if sid in _cancel_tokens:
            _cancel_tokens[sid].cancel("new stream started")

        cancel = CancelToken()
        _cancel_tokens[sid] = cancel

        async def gen():
            handle = Runner.stream(
                agent,
                req.message,
                session=session,
                session_id=sid,
                context_policy=context_policy,
                cancel_token=cancel,
            )
            # Tell the client its session id up front so reconnects work.
            yield {"event": "session", "data": json.dumps({"session_id": sid})}
            final_output: Any = None
            try:
                async for ev in handle:
                    if await request.is_disconnected():
                        cancel.cancel("client disconnected")
                        break
                    needs_approval = isinstance(ev, events.ApprovalRequired)
                    if needs_approval:
                        approvals.register(sid, ev)
                    payload = event_to_sse(ev)
                    if payload is not None:
                        yield payload
                    if isinstance(ev, events.RunCompleted):
                        final_output = ev.result.output
                    if needs_approval:
                        await approvals.await_decision(sid, ev)
            finally:
                await approvals.release(sid)
                _cancel_tokens.pop(sid, None)

            if is_new and generate_titles:
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

    @router.post("/api/chat/cancel")
    async def cancel(session_id: str = Query(...)) -> dict[str, bool]:
        """Cancel an in-progress stream for ``session_id``."""
        token = _cancel_tokens.get(session_id)
        if token is None:
            raise HTTPException(status_code=404, detail="no active stream")
        token.cancel("user requested stop")
        return {"ok": True}

    # ---- sessions -------------------------------------------------------

    @router.get("/api/sessions", response_model=list[ChatSessionInfo])
    async def list_sessions(q: str = Query("", max_length=200)) -> list[ChatSessionInfo]:
        if q:
            return [
                ChatSessionInfo(**m.to_dict())
                for m in await store.search(q, limit=200)
            ]
        return [ChatSessionInfo(**m.to_dict()) for m in await store.list()]

    @router.get("/api/sessions/{session_id}", response_model=SessionDetail)
    async def get_session(session_id: str) -> SessionDetail:
        meta = await store.get(session_id)
        entries = await session.load(session_id)
        msgs = entries_to_messages(entries)

        # Synthesise per-message timestamps by spreading them evenly
        # between created_at and updated_at.
        n = len(msgs)
        t0 = meta.created_at if meta else time.time()
        t1 = meta.updated_at if meta else t0
        if n <= 1:
            spacing = 0.0
        else:
            spacing = max(0.0, (t1 - t0)) / (n - 1)

        body = [
            MessageOut(
                role=m.role,
                content=m.text or m.content,
                reasoning=m.reasoning,
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
                timestamp=t0 + i * spacing,
            )
            for i, m in enumerate(msgs)
        ]
        if meta is None:
            from time import time as _now

            return SessionDetail(
                id=session_id,
                title=None,
                agent=None,
                created_at=_now(),
                updated_at=_now(),
                entries=body,
            )
        return SessionDetail(**meta.to_dict(), entries=body)

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

    @router.get("/api/sessions/{session_id}/export")
    async def export_session(
        session_id: str, format: str = Query("md", pattern="^(md|json|txt)$")
    ) -> Any:
        """Export a session as markdown, JSON, or plain text."""
        meta = await store.get(session_id)
        entries = await session.load(session_id)
        msgs = entries_to_messages(entries)

        if format == "json":
            body = []
            for m in msgs:
                body.append(
                    {
                        "role": m.role,
                        "content": m.text or m.content,
                        "reasoning": m.reasoning,
                        "tool_calls": [
                            {"id": c.id, "name": c.name, "arguments": c.arguments}
                            for c in m.tool_calls
                        ],
                    }
                )
            from fastapi.responses import JSONResponse
            return JSONResponse(
                content={
                    "session_id": session_id,
                    "title": meta.title if meta else None,
                    "agent": meta.agent if meta else None,
                    "messages": body,
                }
            )

        if format == "txt":
            lines: list[str] = []
            for m in msgs:
                role = m.role.upper()
                text = (m.text or m.content) if isinstance(m.text or m.content, str) else str(m.text or m.content or "")
                if text:
                    lines.append(f"## {role}\n\n{text}\n")
                if m.tool_calls:
                    for tc in m.tool_calls:
                        lines.append(f"### Tool: {tc.name}\n```\n{tc.arguments}\n```\n")
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(
                "\n".join(lines),
                headers={"Content-Disposition": f"attachment; filename=lovia-{session_id[:8]}.txt"},
            )

        # Markdown
        lines = []
        title = meta.title if meta else "Chat"
        lines.append(f"# {title}\n")
        lines.append(f"*Session: `{session_id}`*\n")
        for m in msgs:
            role = m.role.capitalize()
            text = (m.text or m.content) if isinstance(m.text or m.content, str) else str(m.text or m.content or "")
            if text:
                lines.append(f"### {role}\n\n{text}\n")
            if m.reasoning:
                lines.append(f"<details>\n<summary>Reasoning</summary>\n\n{m.reasoning}\n\n</details>\n")
            if m.tool_calls:
                for tc in m.tool_calls:
                    lines.append(f"**Tool: `{tc.name}`**\n\n```json\n{tc.arguments}\n```\n")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            "\n".join(lines),
            media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename=lovia-{session_id[:8]}.md"},
        )

    return router
