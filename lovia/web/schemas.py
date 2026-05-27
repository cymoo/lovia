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
