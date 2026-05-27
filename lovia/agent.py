"""Agent definition.

An :class:`Agent` is a **declarative** specification of an LLM-driven actor.
It contains no runtime state: every call to :func:`Runner.run` reads from it
but never mutates it. This makes agents safe to share across requests and
trivial to clone for per-request tweaks.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Generic, TypeVar

from .handoff import Handoff, agent_as_tool
from .providers import ModelSettings, Provider, provider_from_string
from .run_context import RunContext
from .tools import Tool

if TYPE_CHECKING:
    from .guardrails import GuardrailFn
    from .hooks import AgentHooks
    from .mcp import MCPServer
    from .memory import Memory
    from .messages import ToolCall
    from .output import OutputRepairStrategy
    from .skills import SkillCatalog
    from .tools import ToolResultRenderer
    from .tracing import Tracer


TContext = TypeVar("TContext")


# An instructions callable receives the optional user-supplied context and
# returns the system prompt. May be sync or async.
InstructionsFn = Callable[[Any], "str | Awaitable[str]"]

# A programmatic approval handler. Returns True to allow the call, False to
# deny. May be sync or async (the runner awaits the result either way).
ApprovalHandler = Callable[
    ["ToolCall", "RunContext"],
    "bool | Awaitable[bool]",
]


@dataclass
class Agent(Generic[TContext]):
    """Declarative description of an agent.

    The generic parameter ``TContext`` is the type of the optional dependency
    object passed to :meth:`Runner.run` as ``context=...``. Tools annotated
    with ``RunContext[TContext]`` receive a typed context handle.

    Fields:
        name: Human-readable agent name; also used to derive handoff tool names.
        instructions: A static system prompt, or a callable that receives the
            run ``context`` and returns one (sync or async).
        model: Either a ``"vendor:model"`` string (e.g. ``"openai:gpt-4o-mini"``)
            or a pre-built :class:`Provider` instance.
        tools: Tools the agent may call.
        output_type: Pydantic model, dataclass, TypedDict, or builtin type that
            describes the structured final output. ``str`` (the default) means
            free-form text.
        output_repair: When ``True`` (the default), and the model produces an
            output that fails to parse against ``output_type``, the runner
            asks the model once to fix it. Set to ``False`` to fail fast with
            :class:`OutputValidationError`.
        handoffs: Agents (or :class:`Handoff` objects) the model may transfer
            control to via a synthetic ``transfer_to_<name>`` tool.
        settings: Sampling parameters forwarded to the provider.
        skills: Optional :class:`SkillCatalog` exposing on-demand documents.
        mcp_servers: MCP client connections whose tools will be merged at run
            time.
        hooks: Optional :class:`AgentHooks` instance receiving lifecycle events.
        approval_handler: Optional async callable consulted whenever a tool
            with ``needs_approval`` is about to run. Returns ``True`` to allow,
            ``False`` to deny. If ``None`` and no streaming consumer resolves
            the :class:`~lovia.events.ApprovalRequired` event, the call is
            denied by default.
    """

    name: str
    instructions: "str | InstructionsFn" = ""
    model: "str | Provider | list[str | Provider]" = "openai:gpt-4o-mini"
    tools: list[Tool] = field(default_factory=list)
    output_type: Any = str
    # When ``True`` (default), a failed structured-output parse triggers one
    # English repair prompt before giving up. Set to ``False`` to fail fast,
    # or pass an :class:`~lovia.output.OutputRepairStrategy` instance for
    # custom retry policies (multi-attempt, localised prompts, etc.).
    output_repair: "bool | OutputRepairStrategy" = True
    handoffs: list["Agent | Handoff"] = field(default_factory=list)
    settings: ModelSettings = field(default_factory=ModelSettings)
    skills: "SkillCatalog | None" = None
    mcp_servers: list["MCPServer"] = field(default_factory=list)
    hooks: "AgentHooks | None" = None
    approval_handler: ApprovalHandler | None = None
    input_guardrails: list["GuardrailFn"] = field(default_factory=list)
    output_guardrails: list["GuardrailFn"] = field(default_factory=list)
    # Default policies applied to every tool whose own field is ``None``.
    # Tools may still override either knob individually.
    default_tool_retries: int = 1
    default_tool_timeout: float | None = None
    # Optional agent-wide renderer applied to any tool whose own
    # ``result_renderer`` is ``None``. Useful for things like always
    # JSON-serialising via a custom encoder.
    tool_result_renderer: "ToolResultRenderer | None" = None
    # Optional observability + long-term memory hooks. When ``tracer`` is
    # ``None`` the runner uses a no-op tracer, so instrumentation is free.
    tracer: "Tracer | None" = None
    memory: "Memory | None" = None

    def resolve_providers(self) -> list[Provider]:
        """Return the ordered fallback chain of providers for this agent.

        When ``model`` is a single value the chain has length 1. When it is a
        list, each entry is resolved and the runner tries them in order.
        """
        items: list[Any]
        if isinstance(self.model, list):
            items = list(self.model)
        else:
            items = [self.model]
        if not items:
            raise ValueError("Agent.model must not be empty")
        return [provider_from_string(m) if isinstance(m, str) else m for m in items]

    def resolve_provider(self) -> Provider:
        """Return the primary provider (first entry of the fallback chain)."""
        return self.resolve_providers()[0]

    async def render_instructions(self, context: Any) -> str:
        """Materialize the system prompt for a given run."""
        instr = self.instructions
        if callable(instr):
            result = instr(context)
            if hasattr(result, "__await__"):
                return await result  # type: ignore[no-any-return]
            return str(result)
        return str(instr)

    def as_tool(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Tool:
        """Expose this agent as a :class:`Tool` callable by other agents."""
        return agent_as_tool(self, name=name, description=description)

    def clone(self, **overrides: Any) -> "Agent[TContext]":
        """Return a copy of this agent with selected fields overridden."""
        return replace(self, **overrides)
