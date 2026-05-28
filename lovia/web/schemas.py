"""Pydantic request/response schemas for the web API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentInfo(BaseModel):
    name: str
    instructions: str | None = None
    tools: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    agent: str | None = None


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
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class SessionDetail(BaseModel):
    id: str
    title: str | None = None
    agent: str | None = None
    created_at: float
    updated_at: float
    items: list[MessageOut] = Field(default_factory=list)


class ChatSessionInfo(BaseModel):
    id: str
    title: str | None = None
    agent: str | None = None
    created_at: float
    updated_at: float


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class AuditEntry(BaseModel):
    timestamp: float
    agent_name: str
    tool_name: str
    command: str
    verdict: Literal["pass", "warn", "block"]
    reason: str = ""


class FileEntry(BaseModel):
    name: str
    is_dir: bool
    size: int | None = None
    mtime: float | None = None
