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

from ..providers.base import Provider
from ..transcript import TranscriptEntry


@dataclass
class CompactionRequest:
    """Everything a context policy needs to produce a per-call view."""

    entries: list[TranscriptEntry]
    """The full, real transcript. **Read-only** — a policy returns a new
    list for the model call and never mutates ``entries``."""

    provider: Provider | None = None
    """Provider selected for the next model call, if known."""

    model: str | None = None
    """Model name passed to the provider."""

    last_input_tokens: int | None = None
    """Last observed provider input-token count. Lags the current transcript
    by one call; the default pipeline uses it to *calibrate* its estimates
    rather than trusting it directly."""

    overflow: bool = False
    """``True`` when the provider already raised
    :class:`~lovia.ContextOverflowError`; compact more aggressively."""

    reported_window: int | None = None
    """The context window the endpoint named while rejecting the last prompt
    (:attr:`~lovia.ContextOverflowError.reported_window`). Set only on the
    reactive path. The endpoint refusing outranks every other source, so the
    default pipeline remembers it and caps the window with it from then on."""

    scratch: dict[str, Any] = field(default_factory=dict)
    """Mutable state owned by the runner, seeded fresh at run start. A policy
    keeps its derived state here (the default pipeline stores its sticky
    decisions and calibration); the runner round-trips it through the
    checkpoint for resume and persists it to the finished run's
    session-segment ``meta``, so the next run on the same session inherits it
    — no extra hook needed."""


@dataclass
class ContextResult:
    """The per-call view a context policy produced."""

    entries: list[TranscriptEntry]
    """Transcript entries to send to the provider for this call."""

    changed: bool = False
    """Whether ``entries`` differs from the input transcript. With a sticky
    policy this is ``True`` on every call after the first compaction (the
    view replays earlier decisions)."""

    compacted: bool = False
    """Whether **new** compaction decisions were made on *this* call. The
    runner emits :class:`~lovia.events.ContextCompacted` only when this is
    set, so sticky replays don't spam events."""

    reason: str | None = None
    """Stable machine-readable reason for the rewrite (e.g. ``"clear"``,
    ``"offload+summary"``, ``"reactive_summary"``, ``"sticky_replay"``)."""

    summary: str | None = None
    """Summary text newly produced during this call, if any."""

    tokens_before: int | None = None
    """Estimated prompt tokens of the raw transcript."""

    tokens_after: int | None = None
    """Estimated prompt tokens of the returned view."""

    detail: list[str] = field(default_factory=list)
    """Human-readable bullets describing what the policy did this call
    (e.g. ``["2 tool results offloaded"]``), surfaced verbatim in the
    compaction notice and the web UI. Empty for a no-op or a replay."""


class ContextPolicy(Protocol):
    """Strategy that produces the per-call view of the transcript.

    The only required method is :meth:`compact`. A policy may also define
    **optional hooks** the runner reads when present (kept off the protocol so
    a minimal policy needs nothing but ``compact``):

    * ``tools(self) -> list[Tool]`` — extra tools the runner injects whenever
      this policy is active (e.g. the default :class:`Compaction` provides a
      ``recall_tool_result`` bound to its store). A user tool of the same name
      wins; the policy tool is skipped.
    * ``context_window: int | None`` — declaring this attribute tells the
      runner the policy budgets against a window: when its value is ``None``,
      the runner asks the endpoint to report one (a one-shot ``/models``
      probe, memoized per process) before the first model call, so
      ``context_window(req.provider)`` has an answer by the time ``compact``
      runs. A policy *without* the attribute (like :class:`NoopContextPolicy`)
      never needs a window, and the probe is skipped entirely.

    The runner persists ``req.scratch`` verbatim — to the checkpoint for resume,
    and to the finished segment's ``meta`` for the next run on the same session —
    so a policy carries cross-run state simply by writing it there; no extra
    hook, and the same blob restores in both cases.
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
