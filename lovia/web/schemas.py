"""Pydantic request/response schemas for the web API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentInfo(BaseModel):
    name: str
    instructions: str | None = None
    tools: list[str] = Field(default_factory=list)
    # True when the agent has a browsable local workspace (the Files panel
    # shows itself only for such agents).
    workspace: bool = False
    # True when the agent carries a Memory plugin (the sidebar's Memory
    # editor shows itself only for such agents).
    memory: bool = False
    # The model's context window in tokens (server override > agent policy >
    # provider-advertised), or None when unknown — the UI's context ring
    # shows itself only when this is set.
    context_window: int | None = None
    # The model id the agent runs on (a plain string, or the provider's own
    # ``model`` attribute) — shown in the context ring's detail view.
    model: str | None = None


class MemoryNotes(BaseModel):
    """An agent's hot-tier Notes, as shown in the memory editor.

    ``content`` is the canonical ``- fact`` per line markdown body; ``used`` is
    its length in chars against the plugin's ``budget`` (the meter the agent
    itself sees in its prompt).
    """

    content: str
    used: int
    budget: int


class MemoryUpdate(BaseModel):
    """Replace the Notes wholesale with an edited body (see ``MemoryNotes``)."""

    content: str = Field(max_length=1_000_000)


class WorkspaceInfo(BaseModel):
    """The browsable workspace of one agent — just its display name.

    Deliberately NOT the absolute root path: the UI doesn't need it and a
    served page shouldn't advertise server filesystem layout.
    """

    name: str


class WorkspaceEntry(BaseModel):
    """One file/directory in a listing (mirrors ``lovia.workspace.DirEntry``)."""

    path: str
    is_dir: bool
    size: int | None = None
    mtime: float | None = None
    symlink_target: str | None = None


class WorkspaceFile(BaseModel):
    """File content for the viewer (mirrors ``FileContent`` + a binary flag)."""

    path: str
    content: str
    start: int = 1
    end: int = 0
    total_lines: int = 0
    truncated: bool = False
    binary: bool = False


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
    # Applies to new sessions only: an existing session always continues with
    # the agent it was created with (so a stale tab can't switch a chat's brain
    # mid-conversation), falling back to this when that agent is gone.
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
    # True for a ``tool`` message whose stored result was an error, so replayed
    # sessions keep the red error styling the live SSE stream applies.
    is_error: bool = False
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
    status: Literal["running", "blocked_on_approval"]
    turns: int


class RunRecordInfo(BaseModel):
    """A persisted run record, for ``GET /api/runs/history`` and per-schedule
    history — the durable "did that run succeed?" that outlives the process."""

    run_id: str
    session_id: str | None = None
    agent: str | None = None
    # What started the run: "user" | "schedule:<id>".
    source: str
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    error: str | None = None
    started_at: float
    finished_at: float | None = None
    # Token spend, when known: {input_tokens, output_tokens, total_tokens}.
    usage: dict[str, int] | None = None


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
    """Partial update for a schedule — any subset of fields.

    Changing the trigger revalidates it and recomputes ``next_fire``; resuming
    (``active: true``) also recomputes it so stale slots don't fire. Passing
    ``session_id: null`` explicitly detaches the schedule (fresh session per
    fire); omitting the field keeps the current binding.
    """

    input: str | None = Field(default=None, max_length=10_000_000)
    agent: str | None = None
    session_id: str | None = None
    trigger_kind: Literal["cron", "every", "at"] | None = None
    trigger_expr: str | None = Field(default=None, max_length=200)
    active: bool | None = None


class ScheduleInfo(BaseModel):
    id: str
    agent: str | None = None
    input: str
    session_id: str | None = None
    trigger_kind: str
    trigger_expr: str
    next_fire: float
    active: bool
    # Session of the most recent fire — lets a UI link to the run's results.
    last_session_id: str | None = None
    # Outcome of the schedule's most recent run, derived from its run records:
    # "ok" | "error" | None (never fired or still running); ``last_error``
    # carries the message behind an "error".
    last_status: str | None = None
    last_error: str | None = None
    created_at: float
    updated_at: float


class RewindRequest(BaseModel):
    # 0-based index of the user turn to rewind to: everything from that user
    # message onward is dropped (edit-and-resend / regenerate then send a
    # fresh message). Counted over the transcript's user messages in order —
    # the same order a UI renders its user bubbles.
    user_turn: int = Field(ge=0)


class RewindResponse(BaseModel):
    removed: int  # flat transcript entries dropped
    # The authoritative post-rewind view (same shape as SessionDetail.entries)
    # so the client re-renders without a second fetch.
    entries: list[MessageOut] = Field(default_factory=list)


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
