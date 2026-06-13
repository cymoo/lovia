"""Run checkpointing — pause a run mid-flight and resume it later.

A :class:`Checkpointer` snapshots the parts of a run that are safe to
serialize after each turn: the transcript (as :class:`TranscriptEntry` list), the
active agent's name, the accumulated usage, the turn counter, run status,
JSON-safe output/error payloads, and small runner-owned resume state. The opaque
``RunContext.context`` value is *not* snapshotted — callers re-supply it on
resume.

Concrete implementations live in :mod:`lovia.stores`:
:class:`~lovia.stores.InMemoryCheckpointer` for in-process use and
:class:`~lovia.stores.SQLiteCheckpointer` for durable persistence.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from ._types import JsonObject
from .transcript import TranscriptEntry, entry_from_dict, entry_to_dict, to_json_safe
from .messages import Usage

RunStatus = Literal["running", "interrupted", "completed", "failed"]


@dataclass
class RunSnapshot:
    """A serializable snapshot of a run between turns."""

    run_id: str
    agent_name: str
    entries: list[TranscriptEntry]
    usage: Usage
    turns: int
    status: RunStatus = "running"
    output: Any | None = None
    error: JsonObject | None = None
    # Serialized form of the runtime ``ResumeState``: small, JSON-safe
    # runner-owned accumulators that must survive resume (e.g. the context
    # policy's compaction scratch). See ``lovia.runtime.run_state.ResumeState``.
    resume_state: JsonObject = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    # ----- (de)serialization helpers, used by store implementations -----

    def to_dict(self) -> JsonObject:
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "entries": [entry_to_dict(entry) for entry in self.entries],
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cache_read_tokens": self.usage.cache_read_tokens,
                "cache_write_tokens": self.usage.cache_write_tokens,
            },
            "turns": self.turns,
            "status": self.status,
            "output": to_json_safe(self.output),
            "error": to_json_safe(self.error),
            "resume_state": to_json_safe(self.resume_state) or {},
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunSnapshot":
        return cls(
            run_id=data["run_id"],
            agent_name=data["agent_name"],
            entries=[entry_from_dict(entry) for entry in data["entries"]],
            usage=Usage(**data.get("usage", {})),
            turns=data.get("turns", 0),
            status=data["status"],
            output=data.get("output"),
            error=data.get("error"),
            resume_state=data.get("resume_state", {}),
            updated_at=data.get("updated_at", time.time()),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "RunSnapshot":
        return cls.from_dict(json.loads(payload))


@runtime_checkable
class Checkpointer(Protocol):
    """Persist :class:`RunSnapshot` instances keyed by ``run_id``."""

    async def save(self, snapshot: RunSnapshot) -> None: ...

    async def load(self, run_id: str) -> RunSnapshot | None: ...

    async def delete(self, run_id: str) -> None: ...
