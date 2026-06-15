"""Stream events emitted by :meth:`Runner.stream`.

Streaming and observability share the same event types. Events are pure
data — control plumbing (approvals, cancellation) lives elsewhere. See
:mod:`lovia.approvals` for the back-channel used by
:class:`ApprovalRequired`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .types import JsonObject
from .transcript import TranscriptEntry
from .messages import ToolCall

if TYPE_CHECKING:
    from .agent import Agent
    from .approvals import ApprovalChannel
    from .runtime.result import RunResult


@dataclass
class Event:
    """Base class for all events. Useful for ``isinstance`` filtering."""


@dataclass
class RunEvent(Event):
    """Base class for run-level lifecycle events."""


@dataclass
class TurnEvent(Event):
    """Base class for per-turn lifecycle events."""


@dataclass
class DeltaEvent(Event):
    """Base class for streamed model deltas."""


@dataclass
class MessageEvent(Event):
    """Base class for completed assistant-message events."""


@dataclass
class ToolEvent(Event):
    """Base class for tool-call and approval events."""


@dataclass
class TransitionEvent(Event):
    """Base class for agent-transition events."""


@dataclass
class ErrorEvent(Event):
    """Base class for error events."""


@dataclass
class ContextEvent(Event):
    """Base class for context-management events."""


@dataclass
class RunStarted(RunEvent):
    agent: "Agent"


@dataclass
class TurnStarted(TurnEvent):
    agent: "Agent"
    turn: int


@dataclass
class TurnEnded(TurnEvent):
    agent: "Agent"
    turn: int


@dataclass
class TextDelta(DeltaEvent):
    """A partial assistant text fragment emitted during streaming."""

    delta: str


@dataclass
class ReasoningDelta(DeltaEvent):
    """A partial chain-of-thought fragment from providers that expose it.

    Surface in your UI as collapsed/secondary text — these fragments are not
    part of the user-visible response and must not be relied on for behavior.
    """

    delta: str


@dataclass
class MessageCompleted(MessageEvent):
    """One assistant turn fully assembled.

    ``entries`` is the slice of new :class:`TranscriptEntry` values produced by that
    turn — typically a :class:`ReasoningEntry`, a :class:`AssistantTextEntry`,
    and any :class:`ToolCallEntry`\\ s the model requested. Subscribers can
    pattern-match on the concrete entry types.
    """

    entries: list[TranscriptEntry]


@dataclass
class ToolCallStarted(ToolEvent):
    call: ToolCall


@dataclass
class ToolCallCompleted(ToolEvent):
    call: ToolCall
    result: Any
    is_error: bool = False


@dataclass
class HandoffOccurred(TransitionEvent):
    from_agent: "Agent"
    to_agent: "Agent"


@dataclass
class ApprovalRequired(ToolEvent):
    """Emitted before a tool that needs approval runs.

    A streaming consumer resolves the request by calling :meth:`approve` or
    :meth:`reject` on the event (any time before its loop yields control
    back to the runner). Out-of-band callers can resolve by ``ToolCall.id``
    via the :class:`~lovia.approvals.ApprovalChannel` accessible from
    :attr:`RunHandle.approvals`. Setting ``Agent.approval_handler`` provides
    a programmatic policy as a third option.

    If none of those paths resolve the request, the runner defaults to
    **deny** so the run cannot hang.
    """

    call: ToolCall
    # Back-channel reference. Kept private so events stay declarative —
    # callers should prefer ``approve()`` / ``reject()`` on the event or
    # the channel API on ``RunHandle.approvals``.
    _channel: "ApprovalChannel | None" = field(default=None, repr=False)

    def approve(self) -> None:
        """Allow the tool call to proceed."""
        if self._channel is None:
            raise RuntimeError(
                "ApprovalRequired event has no channel attached. "
                "This event was likely constructed outside the runner."
            )
        self._channel.approve(self.call.id)

    def reject(self) -> None:
        """Block the tool call; the model will see a denial message."""
        if self._channel is None:
            raise RuntimeError(
                "ApprovalRequired event has no channel attached. "
                "This event was likely constructed outside the runner."
            )
        self._channel.reject(self.call.id)


@dataclass
class ErrorOccurred(ErrorEvent):
    error: BaseException


@dataclass
class RunCompleted(RunEvent):
    result: "RunResult"


@dataclass
class ContextCompacted(ContextEvent):
    """Emitted when :class:`~lovia.ContextPolicy` produced a compacted view.

    ``entries_before`` is the full transcript; ``entries_after`` is the view the
    runner sent to the provider for this turn. ``summary`` is the model-produced
    summary text when the policy used LLM summarization, or ``None`` for purely
    structural compaction.

    ``reason`` names the policy decision that caused the rewrite.
    ``reactive`` is ``True`` when the compaction was triggered by a
    :class:`~lovia.ContextOverflowError` from the provider rather than by
    the proactive token threshold.

    Compaction is view-only: ``entries_before`` is the full transcript and
    remains the source of truth; ``entries_after`` is the transcript the
    provider saw for this turn only — it is not written back to the Session.
    """

    session_id: str | None
    entries_before: list[TranscriptEntry]
    entries_after: list[TranscriptEntry]
    reason: str
    summary: str | None = None
    reactive: bool = False
    metadata: JsonObject = field(default_factory=dict)
