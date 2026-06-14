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


class SessionDetail(BaseModel):
    id: str
    title: str | None = None
    agent: str | None = None
    created_at: float
    updated_at: float
    entries: list[MessageOut] = Field(default_factory=list)


class ChatSessionInfo(BaseModel):
    id: str
    title: str | None = None
    agent: str | None = None
    created_at: float
    updated_at: float


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class TodoItemOut(BaseModel):
    content: str
    status: str
    active_form: str | None = None


class TodosResponse(BaseModel):
    todos: list[TodoItemOut] = Field(default_factory=list)
