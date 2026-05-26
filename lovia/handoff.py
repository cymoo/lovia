"""Multi-agent: handoff and agent-as-tool.

Two patterns are supported:

* **Handoff**: the current agent calls ``transfer_to_<name>`` to pass control
  to another agent that continues in the same run loop, sharing the message
  history.
* **Agent-as-tool**: an agent is wrapped as a tool; the parent agent calls it
  with a free-form prompt and gets the child's final output back. The child
  runs in its own sub-loop and does not see the parent's history.

Both are implemented as ordinary :class:`Tool` instances so the runner has
exactly one execution path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .tools import Tool

if TYPE_CHECKING:
    from .agent import Agent
    from .runner import RunContext


HANDOFF_TOOL_PREFIX = "transfer_to_"

# Internal sentinel that the runner recognises in a tool result to mean
# "switch the active agent to ``target`` and continue".
@dataclass
class _HandoffSignal:
    target: "Agent"
    reason: str | None = None


@dataclass
class Handoff:
    """A handoff target with optional custom name and description."""

    target: "Agent"
    name: str | None = None
    description: str | None = None
    # Optional callback invoked when the handoff fires; receives the parsed
    # arguments (a single ``reason`` string by default) and the run context.
    on_handoff: Callable[[dict[str, Any], "RunContext"], Awaitable[None] | None] | None = None


def build_handoff_tool(handoff: Handoff) -> Tool:
    """Build the ``transfer_to_<name>`` tool that triggers ``handoff``."""
    target = handoff.target
    tool_name = handoff.name or f"{HANDOFF_TOOL_PREFIX}{_slug(target.name)}"
    description = (
        handoff.description
        or f"Transfer the conversation to the {target.name} agent. Use this when the request matches that agent's specialty."
    )

    async def invoke(args: dict[str, Any], ctx: "RunContext") -> Any:
        if handoff.on_handoff is not None:
            result = handoff.on_handoff(args, ctx)
            if hasattr(result, "__await__"):
                await result  # type: ignore[func-returns-value]
        return _HandoffSignal(target=target, reason=args.get("reason"))

    parameters = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short reason for the handoff.",
            }
        },
        "required": [],
        "additionalProperties": False,
    }

    return Tool(
        name=tool_name,
        description=description,
        parameters=parameters,
        invoke=invoke,
    )


def agent_as_tool(
    agent: "Agent",
    *,
    name: str | None = None,
    description: str | None = None,
) -> Tool:
    """Wrap ``agent`` as a tool callable by another agent.

    The wrapped agent runs as an isolated sub-runner; its result becomes the
    tool's return value (stringified by the runner as usual).
    """
    tool_name = name or f"ask_{_slug(agent.name)}"
    tool_desc = description or f"Delegate a task to the {agent.name} agent and get its answer."

    async def invoke(args: dict[str, Any], ctx: "RunContext") -> Any:
        # Imported here to avoid a circular import at module load time.
        from .runner import Runner

        prompt = args.get("input") or ""
        result = await Runner.run(agent, prompt, context=ctx.context)
        return result.output

    parameters = {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "The task or question to forward to the agent.",
            }
        },
        "required": ["input"],
        "additionalProperties": False,
    }

    return Tool(
        name=tool_name,
        description=tool_desc,
        parameters=parameters,
        invoke=invoke,
    )


def _slug(s: str) -> str:
    """Make a string safe to use as a tool name."""
    out = []
    for ch in s.lower():
        if ch.isalnum() or ch == "_":
            out.append(ch)
        elif ch in (" ", "-"):
            out.append("_")
    return "".join(out) or "agent"
