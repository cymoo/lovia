"""Stream events emitted by :meth:`Runner.run_stream`.

Streaming and observability share the same event types. ``run`` consumes them
internally and dispatches to hooks; ``run_stream`` yields them to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .messages import ChatMessage, ToolCall

if TYPE_CHECKING:
    from .agent import Agent
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

    Application code reads this from the stream, decides, and calls
    :meth:`RunContext.approve` / :meth:`RunContext.reject` on the run context
    (or sets ``approved=True`` on the event before continuing).
    """

    call: ToolCall
    approved: bool | None = None


@dataclass
class ErrorOccurred(Event):
    error: BaseException


@dataclass
class RunCompleted(Event):
    result: "RunResult[Any]"
