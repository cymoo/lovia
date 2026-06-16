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

from .types import JsonObject
from .messages import Message
from .tools import Tool

if TYPE_CHECKING:
    from .agent import Agent
    from .run_context import RunContext


HANDOFF_TOOL_PREFIX = "transfer_to_"


# TODO: handoff中也有target，该类是否有必要存在，把reason放到Handoff里？
# Internal sentinel that the runner recognises in a tool result to mean
# "switch the active agent to ``target`` and continue".
@dataclass
class _HandoffSignal:
    target: "Agent"
    handoff: "Handoff"
    reason: str | None = None


# A function that rewrites the conversation transcript when control is
# transferred. Receives the body of the transcript (everything except the
# leading system prompt, which is re-rendered by the new agent) and returns a
# possibly-filtered version.
HandoffInputFilter = Callable[[list[Message]], list[Message]]


@dataclass
class Handoff:
    """A handoff target with optional customisation.

    Attributes:
        target: The agent to transfer control to.
        name: Override for the ``transfer_to_<name>`` tool name.
        description: Override for the tool description shown to the model.
        on_handoff: Optional callback invoked when the handoff fires; receives
            the parsed arguments (a single ``reason`` string by default) and
            the run context.
        input_filter: Optional function that rewrites the transcript before
            the new agent sees it. Use :func:`drop_stale_tool_calls` to strip
            references to tools the new agent doesn't have.
    """

    target: "Agent"
    name: str | None = None
    description: str | None = None
    on_handoff: (
        Callable[[dict[str, Any], "RunContext"], Awaitable[None] | None] | None
    ) = None
    # TODO: 经过input_filter后的entries，session和checkpoint里存不存？
    input_filter: HandoffInputFilter | None = None


def drop_stale_tool_calls(messages: list[Message]) -> list[Message]:
    """Strip tool calls and tool responses from a transcript.

    A safe default ``input_filter`` for handoffs: keeps user messages,
    assistant text replies, and system messages, but drops references to
    tools the new agent may not have registered. Assistant turns that only
    carried tool calls (no text content) are dropped entirely.
    """
    out: list[Message] = []
    for m in messages:
        if m.role == "tool":
            continue
        if m.role == "assistant" and m.tool_calls:
            if m.content:
                # Preserve text but drop the dangling tool_calls.
                out.append(Message(role="assistant", content=m.content))
            continue
        out.append(m)
    return out


def build_handoff_tool(handoff: Handoff) -> Tool:
    """Build the ``transfer_to_<name>`` tool that triggers ``handoff``."""
    target = handoff.target
    tool_name = handoff.name or f"{HANDOFF_TOOL_PREFIX}{_slug(target.name)}"
    # TODO: description is too simple...
    description = (
        handoff.description
        or f"Transfer the conversation to the {target.name} agent. Use this when the request matches that agent's specialty."
    )

    async def invoke(args: dict[str, Any], ctx: "RunContext") -> Any:
        if handoff.on_handoff is not None:
            result = handoff.on_handoff(args, ctx)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
        return _HandoffSignal(
            target=target,
            handoff=handoff,
            reason=args.get("reason"),
        )

    parameters: JsonObject = {
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
    tool's return value (stringified by the runner as usual). Token usage
    from the sub-run is accumulated into the parent's :class:`Usage` so cost
    reports stay consistent.
    """
    tool_name = name or f"ask_{_slug(agent.name)}"
    tool_desc = (
        description or f"Delegate a task to the {agent.name} agent and get its answer."
    )

    async def invoke(args: dict[str, Any], ctx: "RunContext") -> Any:
        # Imported here to avoid a circular import at module load time.
        from .runner import Runner

        prompt = args.get("input") or ""
        result = await Runner.run(
            agent,
            prompt,
            context=ctx.context,
            _parent_usage=ctx.usage,
        )
        return result.output

    parameters: JsonObject = {
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
