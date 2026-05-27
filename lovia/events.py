"""Stream events emitted by :meth:`Runner.run_stream`.

Streaming and observability share the same event types. Events are pure
data — control plumbing (approvals, cancellation) lives elsewhere. See
:mod:`lovia.approvals` for the back-channel used by
:class:`ApprovalRequired`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .messages import ChatMessage, ToolCall

if TYPE_CHECKING:
    from .agent import Agent
    from .approvals import ApprovalChannel
    from .runner import RunResult


@dataclass
class Event:
    """Base class for all events. Useful for ``isinstance`` filtering."""


@dataclass
class RunStarted(Event):
    agent: "Agent"


@dataclass
class TurnStarted(Event):
    agent: "Agent"
    turn: int


@dataclass
class TurnEnded(Event):
    agent: "Agent"
    turn: int


@dataclass
class TextDelta(Event):
    """A partial assistant text fragment emitted during streaming."""

    delta: str


@dataclass
class ReasoningDelta(Event):
    """A partial chain-of-thought fragment from providers that expose it.

    Surface in your UI as collapsed/secondary text — these fragments are not
    part of the user-visible response and must not be relied on for behavior.
    """

    delta: str


@dataclass
class MessageCompleted(Event):
    """An assistant message fully assembled (may contain tool calls)."""

    message: ChatMessage


@dataclass
class ToolCallStarted(Event):
    call: ToolCall


@dataclass
class ToolCallCompleted(Event):
    call: ToolCall
    result: Any
    is_error: bool = False


@dataclass
class HandoffOccurred(Event):
    from_agent: "Agent"
    to_agent: "Agent"


@dataclass
class ApprovalRequired(Event):
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
class ErrorOccurred(Event):
    error: BaseException


@dataclass
class RunCompleted(Event):
    result: "RunResult"
