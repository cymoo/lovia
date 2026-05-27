"""Stream events emitted by :meth:`Runner.run_stream`.

Streaming and observability share the same event types. ``run`` consumes them
internally and dispatches to hooks; ``run_stream`` yields them to the caller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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

    A streaming consumer resolves the request by calling :meth:`approve` or
    :meth:`reject` on the event (any time before its loop yields control back
    to the runner). Alternatively, set ``Agent.approval_handler`` for a
    programmatic decision.

    If neither path resolves the approval, the runner defaults to **deny**.
    """

    call: ToolCall
    _future: "asyncio.Future[bool] | None" = field(default=None, repr=False)

    def approve(self) -> None:
        """Allow the tool call to proceed."""
        self._resolve(True)

    def reject(self) -> None:
        """Block the tool call; the model will see a denial message."""
        self._resolve(False)

    def _resolve(self, decision: bool) -> None:
        if self._future is None:
            raise RuntimeError(
                "ApprovalRequired event has no future attached. "
                "This event was likely constructed outside the runner."
            )
        if self._future.done():
            return
        self._future.set_result(decision)


@dataclass
class ErrorOccurred(Event):
    error: BaseException


@dataclass
class RunCompleted(Event):
    result: "RunResult[Any]"
