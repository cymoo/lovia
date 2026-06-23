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
from typing import Any, Literal, Protocol

from .types import JsonObject
from .exceptions import UserError
from .transcript import TranscriptEntry, entry_from_dict, entry_to_dict, to_json_safe
from .messages import Usage

RunStatus = Literal["running", "interrupted", "completed", "failed"]
IfRunExists = Literal["resume", "restart", "fail", "resume_only"]

_IF_RUN_EXISTS: set[str] = {"resume", "restart", "fail", "resume_only"}


def _usage_to_dict(usage: Usage) -> JsonObject:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }


@dataclass
class RunHead:
    """The mutable, non-entry state of a run — everything a snapshot carries
    except its ``run_id`` and ``entries``.

    A checkpoint stores entries append-only (one batch per turn) and overwrites
    this small head each turn. Splitting it out keeps :meth:`Checkpointer.append`
    a plain "append these entries, refresh the head" call.
    """

    agent_name: str
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

    def to_dict(self) -> JsonObject:
        return {
            "agent_name": self.agent_name,
            "usage": _usage_to_dict(self.usage),
            "turns": self.turns,
            "status": self.status,
            "output": to_json_safe(self.output),
            "error": to_json_safe(self.error),
            "last_input_tokens": self.last_input_tokens,
            "context_policy_state": to_json_safe(self.context_policy_state) or {},
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunHead":
        return cls(
            agent_name=data["agent_name"],
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
    def from_json(cls, payload: str) -> "RunHead":
        return cls.from_dict(json.loads(payload))


@dataclass
class RunSnapshot:
    """A serializable snapshot of a run between turns.

    ``entries`` is the run's **own** transcript (the input plus everything it
    produced) — *not* the prior session history, which lives in the
    :class:`~lovia.session.Session`. The full transcript on resume is
    ``session.load() + snapshot.entries``.
    """

    run_id: str
    agent_name: str
    entries: list[TranscriptEntry]
    usage: Usage
    turns: int
    status: RunStatus = "running"
    output: Any | None = None
    error: JsonObject | None = None
    last_input_tokens: int | None = None
    context_policy_state: JsonObject = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    # ----- head <-> snapshot, used by store implementations -----

    @property
    def head(self) -> RunHead:
        """The non-entry state, bundled for :meth:`Checkpointer.append`."""
        return RunHead(
            agent_name=self.agent_name,
            usage=self.usage,
            turns=self.turns,
            status=self.status,
            output=self.output,
            error=self.error,
            last_input_tokens=self.last_input_tokens,
            context_policy_state=self.context_policy_state,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_parts(
        cls, run_id: str, entries: list[TranscriptEntry], head: RunHead
    ) -> "RunSnapshot":
        """Rebuild a snapshot from its stored entries and head."""
        return cls(
            run_id=run_id,
            entries=entries,
            agent_name=head.agent_name,
            usage=head.usage,
            turns=head.turns,
            status=head.status,
            output=head.output,
            error=head.error,
            last_input_tokens=head.last_input_tokens,
            context_policy_state=head.context_policy_state,
            updated_at=head.updated_at,
        )

    # ----- whole-snapshot (de)serialization (tests / direct callers) -----

    def to_dict(self) -> JsonObject:
        return {
            "run_id": self.run_id,
            "entries": [entry_to_dict(entry) for entry in self.entries],
            **self.head.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunSnapshot":
        return cls.from_parts(
            run_id=data["run_id"],
            entries=[entry_from_dict(entry) for entry in data["entries"]],
            head=RunHead.from_dict(data),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "RunSnapshot":
        return cls.from_dict(json.loads(payload))


class Checkpointer(Protocol):
    """Persist a run keyed by ``run_id`` as an append-only entry log + head.

    Symmetric with :class:`~lovia.session.Session`: ``append`` adds a turn's
    entries and refreshes the small mutable :class:`RunHead`; ``load``
    reconstructs the whole :class:`RunSnapshot`.
    """

    async def append(
        self, run_id: str, entries: list[TranscriptEntry], head: RunHead
    ) -> None:
        """Append this turn's ``entries`` and overwrite the run's ``head``.

        ``entries`` may be empty (a head-only refresh, e.g. on completion).
        Entries already stored for ``run_id`` are never rewritten — the run's
        transcript only grows.
        """
        ...

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
