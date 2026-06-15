"""Run checkpointing — pause a run mid-flight and resume it later.

A :class:`Checkpointer` snapshots the parts of a run that are safe to
serialize after each turn: the transcript (as :class:`TranscriptEntry` list), the
active agent's name, the accumulated usage, the turn counter, run status,
JSON-safe output/error payloads, the last observed input-token count, and the
context policy's opaque per-run state. The opaque ``RunContext.context`` value
is *not* snapshotted — callers re-supply it on resume.

Concrete implementations live in :mod:`lovia.stores`:
:class:`~lovia.stores.InMemoryCheckpointer` for in-process use and
:class:`~lovia.stores.SQLiteCheckpointer` for durable persistence.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from .types import JsonObject
from .exceptions import UserError
from .transcript import TranscriptEntry, entry_from_dict, entry_to_dict, to_json_safe
from .messages import Usage

RunStatus = Literal["running", "interrupted", "completed", "failed"]
IfRunExists = Literal["resume", "restart", "fail", "require"]

_IF_RUN_EXISTS: set[str] = {"resume", "restart", "fail", "require"}


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
    # Last observed provider input-token count; lets the context policy
    # calibrate its estimates on the first post-resume turn.
    last_input_tokens: int | None = None
    # Opaque per-run state owned by the context policy (sticky compaction
    # decisions, calibrated ratio, running summary, …). The runner never
    # inspects this — it round-trips it through checkpoints so the policy
    # can pick up where it left off after a resume.
    context_policy_state: JsonObject = field(default_factory=dict)
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
            "last_input_tokens": self.last_input_tokens,
            "context_policy_state": to_json_safe(self.context_policy_state) or {},
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
            last_input_tokens=data.get("last_input_tokens"),
            context_policy_state=data.get("context_policy_state", {}),
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


@dataclass(frozen=True, slots=True)
class CheckpointOptions:
    """Checkpoint/resume configuration for a single runner invocation.

    A normal durable run supplies ``checkpointer`` and ``run_id``. Advanced
    callers may pass ``resume_from`` directly; when ``run_id`` is omitted, the
    snapshot's own id becomes the run id.
    """

    checkpointer: Checkpointer | None = None
    run_id: str | None = None
    if_run_exists: IfRunExists = "resume"
    delete_on_success: bool = False
    resume_from: RunSnapshot | None = None

    def __post_init__(self) -> None:
        if self.if_run_exists not in _IF_RUN_EXISTS:
            raise UserError(
                f"if_run_exists must be one of {sorted(_IF_RUN_EXISTS)!r}",
            )
        if self.run_id is not None and (
            not isinstance(self.run_id, str) or not self.run_id.strip()
        ):
            raise UserError("checkpoint run_id must be a non-empty string")
        if self.resume_from is None:
            if self.checkpointer is None:
                raise UserError(
                    "checkpoint requires both checkpointer and run_id, "
                    "or a resume_from snapshot"
                )
            if self.run_id is None:
                raise UserError("checkpoint run_id is required with checkpointer")
        elif self.run_id is not None and self.run_id != self.resume_from.run_id:
            raise UserError(
                f"checkpoint run_id {self.run_id!r} does not match "
                f"resume_from.run_id {self.resume_from.run_id!r}"
            )
        if self.checkpointer is None and self.delete_on_success:
            raise UserError(
                "checkpoint delete_on_success requires a checkpointer",
            )

    @property
    def resolved_run_id(self) -> str:
        """The id used for tracing, snapshot lookup, and snapshot writes."""
        if self.run_id is not None:
            return self.run_id
        assert self.resume_from is not None
        return self.resume_from.run_id
