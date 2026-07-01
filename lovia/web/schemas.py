"""Pydantic request/response schemas for the web API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentInfo(BaseModel):
    name: str
    instructions: str | None = None
    tools: list[str] = Field(default_factory=list)


class ServerInfo(BaseModel):
    """Server-level capabilities, for a custom UI to introspect on load."""

    title: str
    agents: list[str] = Field(default_factory=list)
    default_agent: str | None = None
    version: str | None = None
    features: dict[str, bool] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    # Sanity bound on message size, deliberately generous: this server is driven
    # with very large prompts (up to ~1M-token contexts), so a small cap would
    # reject legitimate input. Not a fast-fail DoS guard — Starlette buffers the
    # body before validation — so true body-size limits belong at the ASGI layer
    # (behind a reverse proxy).
    message: str = Field(max_length=10_000_000)
    session_id: str | None = None
    agent: str | None = None


class InjectRequest(BaseModel):
    session_id: str
    message: str = Field(max_length=10_000_000)


class InjectCancelRequest(BaseModel):
    session_id: str
    id: int


class ChatResponse(BaseModel):
    output: Any
    session_id: str | None
    usage: dict[str, int]


class MarkdownRequest(BaseModel):
    text: str = Field(max_length=200_000)


class MarkdownResponse(BaseModel):
    html: str


class ApprovalRequest(BaseModel):
    session_id: str
    call_id: str
    decision: Literal["approve", "deny"]


class MessageOut(BaseModel):
    role: str
    content: Any
    reasoning: str | None = None
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    timestamp: float | None = None
    # Populated only for a synthetic ``role="context_compacted"`` entry: the
    # persisted compaction notice ({reason, reactive, summary, tokens_before,
    # tokens_after, detail}) that ``renderHistory`` replays. ``None`` for every
    # real message.
    compaction: dict[str, Any] | None = None


class SessionDetail(BaseModel):
    id: str
    title: str | None = None
    agent: str | None = None
    created_at: float
    updated_at: float
    entries: list[MessageOut] = Field(default_factory=list)
    active_run_id: str | None = None


class ChatSessionInfo(BaseModel):
    id: str
    title: str | None = None
    agent: str | None = None
    created_at: float
    updated_at: float
    pinned: bool = False


class RunInfo(BaseModel):
    """A live supervised (background) run, for ``GET /api/runs``."""

    session_id: str
    run_id: str | None = None
    agent: str
    status: str
    turns: int


class ScheduleSpec(BaseModel):
    """Create a scheduled background run.

    ``trigger_expr`` is the cron string (``cron``), interval seconds (``every``),
    or epoch timestamp (``at``). With ``session_id`` the fire continues that
    conversation; without it, a fresh session is created per fire.
    """

    input: str = Field(max_length=10_000_000)
    agent: str | None = None
    session_id: str | None = None
    trigger_kind: Literal["cron", "every", "at"]
    trigger_expr: str = Field(max_length=200)


class SchedulePatch(BaseModel):
    active: bool


class ScheduleInfo(BaseModel):
    id: str
    agent: str | None = None
    input: str
    session_id: str | None = None
    trigger_kind: str
    trigger_expr: str
    next_fire: float
    active: bool
    created_at: float
    updated_at: float


class SessionPatch(BaseModel):
    """Partial update for a session — rename, (un)pin, or both."""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None


class TodoItemOut(BaseModel):
    content: str
    status: str
    active_form: str | None = None


class TodosResponse(BaseModel):
    todos: list[TodoItemOut] = Field(default_factory=list)
