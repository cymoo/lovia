"""Run checkpointing — pause a run mid-flight and resume it later.

A :class:`Checkpointer` snapshots the parts of a run that are safe to
serialize after each turn: the transcript, the active agent's name, the
accumulated usage, and turn counter. The opaque ``RunContext.context``
value is *not* snapshotted — callers re-supply it on resume.

The default in-process implementation is :class:`InMemoryCheckpointer`;
:class:`~lovia.stores.sqlite_checkpointer.SQLiteCheckpointer` persists
snapshots to a SQLite file.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .messages import ChatMessage, ToolCall, Usage


@dataclass
class RunSnapshot:
    """A serializable snapshot of a run between turns."""

    run_id: str
    agent_name: str
    messages: list[ChatMessage]
    usage: Usage
    turns: int
    updated_at: float = field(default_factory=time.time)

    # ----- (de)serialization helpers, used by store implementations -----

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "messages": [_message_to_dict(m) for m in self.messages],
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cache_read_tokens": self.usage.cache_read_tokens,
                "cache_write_tokens": self.usage.cache_write_tokens,
            },
            "turns": self.turns,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunSnapshot":
        return cls(
            run_id=data["run_id"],
            agent_name=data["agent_name"],
            messages=[_message_from_dict(m) for m in data["messages"]],
            usage=Usage(**data.get("usage", {})),
            turns=data.get("turns", 0),
            updated_at=data.get("updated_at", time.time()),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "RunSnapshot":
        return cls.from_dict(json.loads(payload))


def _message_to_dict(msg: ChatMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.role}
    # Content may be a string or list of content blocks; for blocks we fall
    # back to their pydantic dump so multimodal content survives a round-trip.
    if isinstance(msg.content, list):
        out["content"] = [
            block.model_dump() if hasattr(block, "model_dump") else block
            for block in msg.content
        ]
    else:
        out["content"] = msg.content
    if msg.tool_calls:
        out["tool_calls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments}
            for c in msg.tool_calls
        ]
    if msg.tool_call_id:
        out["tool_call_id"] = msg.tool_call_id
    if msg.name:
        out["name"] = msg.name
    if msg.reasoning_content:
        out["reasoning_content"] = msg.reasoning_content
    return out


def _message_from_dict(data: dict[str, Any]) -> ChatMessage:
    from .content import TextBlock, ImageBlock  # local import: cheap, avoid cycle

    raw_content = data.get("content")
    content: Any
    if isinstance(raw_content, list):
        rebuilt: list[Any] = []
        for block in raw_content:
            if isinstance(block, dict) and block.get("type") == "image":
                rebuilt.append(ImageBlock(**block))
            elif isinstance(block, dict) and block.get("type") == "text":
                rebuilt.append(TextBlock(**block))
            else:
                rebuilt.append(block)
        content = rebuilt
    else:
        content = raw_content

    return ChatMessage(
        role=data["role"],
        content=content,
        tool_calls=[ToolCall(**c) for c in data.get("tool_calls", [])],
        tool_call_id=data.get("tool_call_id"),
        name=data.get("name"),
        reasoning_content=data.get("reasoning_content"),
    )


@runtime_checkable
class Checkpointer(Protocol):
    """Persist :class:`RunSnapshot` instances keyed by ``run_id``."""

    async def save(self, snapshot: RunSnapshot) -> None: ...

    async def load(self, run_id: str) -> RunSnapshot | None: ...

    async def delete(self, run_id: str) -> None: ...


class InMemoryCheckpointer:
    """Trivial in-process checkpointer. Useful for tests and short-lived runs."""

    def __init__(self) -> None:
        self._snapshots: dict[str, RunSnapshot] = {}

    async def save(self, snapshot: RunSnapshot) -> None:
        self._snapshots[snapshot.run_id] = snapshot

    async def load(self, run_id: str) -> RunSnapshot | None:
        return self._snapshots.get(run_id)

    async def delete(self, run_id: str) -> None:
        self._snapshots.pop(run_id, None)
