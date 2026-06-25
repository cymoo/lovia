"""The context-policy contract between the runner and compaction strategies.

A :class:`ContextPolicy` turns the full transcript into the (smaller) list of
entries sent to the provider for *one* model call. It never mutates the
transcript and never writes to the :class:`~lovia.Session` — the real
conversation remains the single source of truth, so a bad compaction can only
affect model calls, never stored history.

The default implementation is :class:`~lovia.context.Compaction`; see
:mod:`lovia.context` for the full picture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..types import JsonObject
from ..providers.base import Provider
from ..transcript import TranscriptEntry


@dataclass
class CompactionRequest:
    """Everything a context policy needs to produce a per-call view.

    Attributes:
        entries: The full, real transcript. **Read-only** — a policy returns a
            new list for the model call and never mutates ``entries``.
        provider: Provider selected for the next model call, if known.
        model: Model name passed to the provider.
        last_input_tokens: Last observed provider input-token count. Lags the
            current transcript by one call; the default pipeline uses it to
            *calibrate* its estimates rather than trusting it directly.
        overflow: ``True`` when the provider already raised
            :class:`~lovia.ContextOverflowError`; the policy should compact
            more aggressively.
        scratch: Per-run mutable state owned by the runner. A policy may keep
            derived state here (the default pipeline stores its sticky
            decisions) without leaking it across runs — the runner creates a
            fresh dict for each run and round-trips it through checkpoints.
    """

    entries: list[TranscriptEntry]
    provider: Provider | None = None
    model: str | None = None
    last_input_tokens: int | None = None
    overflow: bool = False
    scratch: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextResult:
    """The per-call view a context policy produced.

    Attributes:
        entries: Transcript entries to send to the provider for this call.
        changed: Whether ``entries`` differs from the input transcript. With
            a sticky policy this is ``True`` on every call after the first
            compaction (the view replays earlier decisions).
        compacted: Whether **new** compaction decisions were made on *this*
            call. The runner emits :class:`~lovia.events.ContextCompacted`
            only when this is set, so sticky replays don't spam events.
        reason: Stable machine-readable reason for the rewrite (e.g.
            ``"clear"``, ``"offload+summary"``, ``"reactive_summary"``,
            ``"sticky_replay"``).
        summary: Summary text newly produced during this call, if any.
        tokens_before: Estimated prompt tokens of the raw transcript.
        tokens_after: Estimated prompt tokens of the returned view.
        metadata: Extra diagnostic details emitted with compaction events.
    """

    entries: list[TranscriptEntry]
    changed: bool = False
    compacted: bool = False
    reason: str | None = None
    summary: str | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None
    metadata: JsonObject = field(default_factory=dict)


class ContextPolicy(Protocol):
    """Strategy that produces the per-call view of the transcript.

    The only required method is :meth:`compact`. A policy may also define two
    **optional hooks** the runner invokes when present (kept off the protocol
    so a minimal policy needs nothing but ``compact``):

    * ``tools(self) -> list[Tool]`` — extra tools the runner injects whenever
      this policy is active (e.g. the default :class:`Compaction` provides a
      ``recall_tool_result`` bound to its store). A user tool of the same name
      wins; the policy tool is skipped.
    * ``carryover(self, scratch) -> JsonObject | None`` — the cross-run subset
      of ``scratch`` to persist in the finished run's session-segment ``meta``,
      so a follow-up run on the same session resumes the policy's decisions
      without re-deriving them. Return ``None`` for nothing to carry.
    """

    async def compact(self, req: CompactionRequest) -> ContextResult:
        """Return the view to send to the provider for the next model call.

        Must not mutate ``req.entries`` — the result is used only for one
        provider call. Durable decisions belong in ``req.scratch``.
        """
        ...


class NoopContextPolicy:
    """A context policy that never modifies the transcript."""

    name = "noop"

    async def compact(self, req: CompactionRequest) -> ContextResult:
        """Return ``req.entries`` unchanged."""
        return ContextResult(entries=req.entries)


__all__ = [
    "CompactionRequest",
    "ContextPolicy",
    "ContextResult",
    "NoopContextPolicy",
]
