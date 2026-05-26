"""Lifecycle hooks for observability and side effects.

A single :class:`AgentHooks` instance is attached to an :class:`Agent`. The
runner dispatches every stream event to the corresponding ``on_*`` method, if
defined. All methods are optional and default to no-ops, so users only
implement what they care about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import events

if TYPE_CHECKING:
    from .agent import Agent
    from .messages import ChatMessage, ToolCall
    from .runner import RunResult


class AgentHooks:
    """Base hook implementation. Override any subset of methods."""

    async def on_run_started(self, agent: "Agent") -> None: ...

    async def on_run_completed(self, result: "RunResult[Any]") -> None: ...

    async def on_turn_started(self, agent: "Agent", turn: int) -> None: ...

    async def on_turn_ended(self, agent: "Agent", turn: int) -> None: ...

    async def on_text_delta(self, delta: str) -> None: ...

    async def on_message(self, message: "ChatMessage") -> None: ...

    async def on_tool_call_started(self, call: "ToolCall") -> None: ...

    async def on_tool_call_completed(
        self, call: "ToolCall", result: Any, is_error: bool
    ) -> None: ...

    async def on_handoff(self, from_agent: "Agent", to_agent: "Agent") -> None: ...

    async def on_approval_required(self, call: "ToolCall") -> None: ...

    async def on_error(self, error: BaseException) -> None: ...


async def dispatch(hooks: AgentHooks | None, event: events.Event) -> None:
    """Route an event to the right ``AgentHooks`` method."""
    if hooks is None:
        return

    if isinstance(event, events.RunStarted):
        await hooks.on_run_started(event.agent)
    elif isinstance(event, events.RunCompleted):
        await hooks.on_run_completed(event.result)
    elif isinstance(event, events.TurnStarted):
        await hooks.on_turn_started(event.agent, event.turn)
    elif isinstance(event, events.TurnEnded):
        await hooks.on_turn_ended(event.agent, event.turn)
    elif isinstance(event, events.TextDelta):
        await hooks.on_text_delta(event.delta)
    elif isinstance(event, events.MessageCompleted):
        await hooks.on_message(event.message)
    elif isinstance(event, events.ToolCallStarted):
        await hooks.on_tool_call_started(event.call)
    elif isinstance(event, events.ToolCallCompleted):
        await hooks.on_tool_call_completed(event.call, event.result, event.is_error)
    elif isinstance(event, events.HandoffOccurred):
        await hooks.on_handoff(event.from_agent, event.to_agent)
    elif isinstance(event, events.ApprovalRequired):
        await hooks.on_approval_required(event.call)
    elif isinstance(event, events.ErrorOccurred):
        await hooks.on_error(event.error)
