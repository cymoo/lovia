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
from .tools import Tool
from .reliability import RetryPolicy

if TYPE_CHECKING:
    from .agent import Agent
    from .run_context import RunContext
    from .reliability import RunBudget
    from .context.policy import ContextPolicy


HANDOFF_TOOL_PREFIX = "transfer_to_"


# Internal sentinel that the runner recognises in a tool result to mean
# "switch the active agent to ``handoff.target`` and continue". ``reason`` is
# per-invocation (from the model's tool arguments), so it rides on the signal
# rather than the shared ``Handoff`` config.
@dataclass
class _HandoffSignal:
    handoff: "Handoff"
    reason: str | None = None


@dataclass
class Handoff:
    """A handoff target with optional customisation.

    Attributes:
        target: The agent to transfer control to.
        name: Override for the ``transfer_to_<name>`` tool name.
        description: Override for the tool description shown to the model. This
            is the routing signal the parent agent sees — set it to the target's
            specialty when the default ("Transfer to the <name> agent...") is too
            thin to route on reliably.
        on_handoff: Optional callback invoked when the handoff fires; receives
            the parsed arguments (a single ``reason`` string by default) and
            the run context.
    """

    target: "Agent[Any]"
    name: str | None = None
    description: str | None = None
    on_handoff: (
        Callable[[dict[str, Any], "RunContext[Any]"], Awaitable[None] | None] | None
    ) = None


def build_handoff_tool(handoff: Handoff) -> Tool:
    """Build the ``transfer_to_<name>`` tool that triggers ``handoff``."""
    target = handoff.target
    tool_name = handoff.name or f"{HANDOFF_TOOL_PREFIX}{_slug(target.name)}"
    # The default description is deliberately generic: the agent name alone is a
    # thin routing signal. When the parent must choose between similar agents,
    # set ``Handoff.description`` with the target's specialty — that is the knob
    # for routing, rather than embedding ``target.instructions`` (which may be a
    # callable or a large system prompt and would bloat the parent's schema).
    description = (
        handoff.description
        or f"Transfer the conversation to the {target.name} agent. Use this when the request matches that agent's specialty."
    )

    async def invoke(args: dict[str, Any], ctx: "RunContext[Any]") -> Any:
        if handoff.on_handoff is not None:
            result = handoff.on_handoff(args, ctx)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
        return _HandoffSignal(handoff=handoff, reason=args.get("reason"))

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
    agent: "Agent[Any]",
    *,
    name: str | None = None,
    description: str | None = None,
    max_turns: int = 50,
    budget: "RunBudget | None" = None,
    retry: "RetryPolicy | None" = RetryPolicy(),
    context_policy: "ContextPolicy | None" = None,
) -> Tool:
    """Wrap ``agent`` as a tool callable by another agent.

    The wrapped agent runs as an isolated sub-runner; its result becomes the
    tool's return value (stringified by the runner as usual). Token usage
    from the sub-run is accumulated into the parent's :class:`Usage` so cost
    reports stay consistent.

    The execution-policy keywords (``max_turns``, ``budget``, ``retry``,
    ``context_policy``) are fixed here by the developer and forwarded to the
    sub-run; they are *not* exposed to the model, which only controls the
    free-form ``input``. Bound ``max_turns`` especially: a delegated sub-agent
    loops on its own, and the run default is generous. The sub-run inherits the
    parent's ``context`` and accumulates into its :class:`Usage` automatically.

    The sub-run also inherits the parent's ``cancel_token``: cancellation is
    cooperative, so while the parent is blocked awaiting this sub-run only the
    child is checking the token. Sharing the instance lets a ``cancel()`` trip
    the child at its next turn boundary; the resulting :class:`RunCancelled`
    propagates straight up through the tool call and terminates the parent run.
    """
    tool_name = name or f"ask_{_slug(agent.name)}"
    tool_desc = (
        description or f"Delegate a task to the {agent.name} agent and get its answer."
    )

    async def invoke(args: dict[str, Any], ctx: "RunContext[Any]") -> Any:
        # Imported here to avoid a circular import at module load time.
        from .runner import Runner

        prompt = args.get("input") or ""
        result = await Runner.run(
            agent,
            prompt,
            context=ctx.context,
            max_turns=max_turns,
            budget=budget,
            cancel_token=ctx.cancel_token,
            retry=retry,
            context_policy=context_policy,
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
