"""Agent definition.

An :class:`Agent` is a **declarative** specification of an LLM-driven actor.
It contains no runtime state: every call to :func:`Runner.run` reads from it
but never mutates it. This makes agents safe to share across requests and
trivial to clone for per-request tweaks.

The one exception to "no runtime state" is the ``_fragments`` tuple populated
by the :meth:`system_prompt` decorator — but it's still configuration, not
session data, and clone operations copy it immutably.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Generic,
    Literal,
    TypeVar,
    Union,
)

from .handoff import Handoff, agent_as_tool
from .providers import ModelSettings, Provider, provider_from_string
from .run_context import RunContext
from .tools import Tool

if TYPE_CHECKING:
    from .guardrails import GuardrailFn
    from .hooks import AgentHooks
    from .messages import Message, ToolCall
    from .output import OutputRepairStrategy
    from .plugins import Plugin
    from .runtime.result import RunHandle, RunResult
    from .tools import ToolResultRenderer
    from .workspace import WorkspaceLike
    from .tracing import Tracer


TContext = TypeVar("TContext")


# An instructions callable receives the optional user-supplied context and
# returns the system prompt. May be sync or async.
InstructionsFn = Callable[[Any], "str | Awaitable[str]"]

# A programmatic approval decision: ``True``/``"allow"`` permits the call,
# ``False``/``"deny"`` blocks it, and ``"ask"`` defers to the streaming
# consumer (the call is denied if nobody resolves it — fail closed).
ApprovalDecision = Union[bool, Literal["allow", "deny", "ask"]]

# A programmatic approval handler consulted for tools gated by
# ``needs_approval``. May be sync or async (the runner awaits the result
# either way).
ApprovalHandler = Callable[
    ["ToolCall", "RunContext"],
    "ApprovalDecision | Awaitable[ApprovalDecision]",
]


@dataclass
class Agent(Generic[TContext]):
    """Declarative description of an agent.

    The generic parameter ``TContext`` is the type of the optional dependency
    object passed to :meth:`Runner.run` as ``context=...``. Tools annotated
    with ``RunContext[TContext]`` receive a typed context handle.

    Fields:
        name: Human-readable agent name; also used to derive handoff tool names.
        instructions: A static base system prompt, or a callable that
            receives the run ``context`` and returns one (sync or async).
            Additional dynamic fragments can be registered with the
            :meth:`system_prompt` decorator and appended at render time.
        model: Either a ``"vendor:model"`` string (e.g. ``"openai:gpt-5.4"``)
            or a pre-built :class:`Provider` instance.
        tools: Tools the agent may call.
        output_type: Pydantic model, dataclass, TypedDict, or builtin type that
            describes the structured final output. ``str`` (the default) means
            free-form text. :meth:`Runner.run` may override this per call.
        output_repair: When ``True`` (the default), and the model produces an
            output that fails to parse against ``output_type``, the runner
            asks the model once to fix it. Set to ``False`` to fail fast with
            :class:`OutputValidationError`.
        handoffs: Agents (or :class:`Handoff` objects) the model may transfer
            control to via a synthetic ``transfer_to_<name>`` tool.
        settings: Sampling parameters forwarded to the provider.
        workspace: Optional :class:`~lovia.workspace.Workspace` (or anything
            implementing ``WorkspaceLike``) scoping file/shell tools to a
            directory and policy. Its tool bundle is merged at run time and
            its live session is injected into ``RunContext.workspace``.
        hooks: Optional :class:`AgentHooks` instance receiving lifecycle events.
        approval_handler: Optional callable consulted whenever a tool with
            ``needs_approval`` is about to run. Returns an
            :data:`ApprovalDecision` — ``True``/``"allow"`` to permit,
            ``False``/``"deny"`` to block, ``"ask"`` to defer to the streaming
            consumer. If ``None`` and no streaming consumer resolves the
            :class:`~lovia.events.ApprovalRequired` event, the call is denied
            by default.
    """

    name: str
    instructions: "str | InstructionsFn" = ""
    model: "str | Provider | list[str | Provider]" = "openai:gpt-5.4"
    tools: list[Tool] = field(default_factory=list)
    output_type: Any = str
    # When ``True`` (default), a failed structured-output parse triggers one
    # English repair prompt before giving up. Set to ``False`` to fail fast,
    # or pass an :class:`~lovia.output.OutputRepairStrategy` instance for
    # custom retry policies (multi-attempt, localised prompts, etc.).
    output_repair: "bool | OutputRepairStrategy" = True
    handoffs: list["Agent | Handoff"] = field(default_factory=list)
    settings: ModelSettings = field(default_factory=ModelSettings)
    workspace: "WorkspaceLike | None" = None
    # Declarative features that bundle tools, per-turn view injectors, static
    # system-prompt text, and event hooks. Each is activated once per run (and
    # per agent on a handoff). See :mod:`lovia.plugins` and
    # :func:`lovia.plugins.todos`.
    plugins: list["Plugin"] = field(default_factory=list)
    hooks: "AgentHooks | None" = None
    approval_handler: ApprovalHandler | None = None
    input_guardrails: list["GuardrailFn"] = field(default_factory=list)
    output_guardrails: list["GuardrailFn"] = field(default_factory=list)
    # Default policies applied to every tool whose own field is ``None``.
    # Tools may still override either knob individually.
    default_tool_retries: int = 0
    default_tool_timeout: float | None = None
    # Cap on the rendered tool-output string stored in the transcript, in
    # characters. Anything longer is truncated (head + tail kept, with a
    # marker) *before* it enters the transcript, and the raw return value is
    # dropped — bounding memory, checkpoint, and session cost for tools that
    # can return huge payloads. Lossy by design: the cut middle is gone (the
    # ``recall_tool_result`` tool sees the truncated version too); tools that
    # need full-fidelity recovery should write to the workspace themselves.
    # ``None`` (default) stores outputs in full. Per-tool ``max_output_chars``
    # overrides this.
    max_tool_output_chars: int | None = None
    # Optional agent-wide renderer applied to any tool whose own
    # ``result_renderer`` is ``None``. Useful for things like always
    # JSON-serialising via a custom encoder.
    tool_result_renderer: "ToolResultRenderer | None" = None
    # Optional tracer. When ``None`` the runner uses a no-op tracer, so
    # instrumentation is free.
    tracer: "Tracer | None" = None
    # Dynamic system-prompt fragments registered via @agent.system_prompt.
    # Rendered in registration order and appended after ``instructions``.
    # Not a public field — use the decorator or ``with_system_prompt`` instead.
    _fragments: tuple[InstructionsFn, ...] = field(
        default_factory=tuple, repr=False, compare=False
    )

    def resolve_providers(self) -> list[Provider]:
        """Return the ordered fallback chain of providers for this agent.

        When ``model`` is a single value the chain has length 1. When it is a
        list, each entry is resolved and the runner tries them in order.
        """
        models: list[Any]
        if isinstance(self.model, list):
            models = list(self.model)
        else:
            models = [self.model]
        if not models:
            raise ValueError("Agent.model must not be empty")
        return [provider_from_string(m) if isinstance(m, str) else m for m in models]

    def resolve_provider(self) -> Provider:
        """Return the primary provider (first entry of the fallback chain)."""
        return self.resolve_providers()[0]

    # ------------------------------------------------------------------ #
    # Dynamic system-prompt fragments
    # ------------------------------------------------------------------ #

    def system_prompt(self, fn: InstructionsFn) -> InstructionsFn:
        """Register a dynamic system-prompt fragment.

        The decorated callable receives the run context and returns (or
        awaits) a string. All fragments are concatenated to ``instructions``
        at render time, in registration order, separated by blank lines.
        Returning an empty string skips the fragment.

        Example::

            agent = Agent(name="x", instructions="You are helpful.")

            @agent.system_prompt
            async def add_user_tier(ctx) -> str:
                return f"User tier: {ctx.context.tier}"
        """
        self._fragments = (*self._fragments, fn)
        return fn

    def with_system_prompt(self, fn: InstructionsFn) -> "Agent[TContext]":
        """Return a clone with one additional dynamic system-prompt fragment."""
        new = self.clone()
        new._fragments = (*self._fragments, fn)
        return new

    async def render_instructions(
        self, context: Any, *, extra: "str | InstructionsFn | None" = None
    ) -> str:
        """Materialize the system prompt for a given run.

        Concatenates: the base ``instructions`` (str or callable), every
        fragment registered via :meth:`system_prompt`, and finally ``extra``
        (a per-call addendum supplied by the runner). Empty results are
        skipped so users can return ``""`` to opt out conditionally.
        """
        parts: list[str] = []

        async def render(fragment: "str | InstructionsFn | None") -> str:
            if fragment is None:
                return ""
            if callable(fragment):
                result = fragment(context)
                if hasattr(result, "__await__"):
                    return str(await result)  # type: ignore[arg-type]
                return str(result)
            return str(fragment)

        base = await render(self.instructions)
        if base:
            parts.append(base)
        for frag in self._fragments:
            text = await render(frag)
            if text:
                parts.append(text)
        if extra is not None:
            extra_text = await render(extra)
            if extra_text:
                parts.append(extra_text)
        return "\n\n".join(parts)

    def as_tool(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Tool:
        """Expose this agent as a :class:`Tool` callable by other agents."""
        return agent_as_tool(self, name=name, description=description)

    def clone(self, **overrides: Any) -> "Agent[TContext]":
        """Return a copy of this agent with selected fields overridden.

        Dynamic system-prompt fragments registered on the source agent are
        copied immutably so the clone inherits them without sharing mutable
        prompt state.
        """
        new = replace(self, **overrides)
        new._fragments = self._fragments
        return new

    # ------------------------------------------------------------------ #
    # Convenience instance methods — thin wrappers over Runner
    # ------------------------------------------------------------------ #

    async def run(
        self,
        input: "str | list[Message]",
        **kwargs: Any,
    ) -> "RunResult":
        """Shortcut for ``Runner.run(self, input, **kwargs)``."""
        from .runner import Runner

        return await Runner.run(self, input, **kwargs)

    def run_sync(
        self,
        input: "str | list[Message]",
        **kwargs: Any,
    ) -> "RunResult":
        """Synchronous shortcut for ``Runner.run_sync(self, input, **kwargs)``."""
        from .runner import Runner

        return Runner.run_sync(self, input, **kwargs)

    def stream(
        self,
        input: "str | list[Message]",
        **kwargs: Any,
    ) -> "RunHandle":
        """Shortcut for ``Runner.stream(self, input, **kwargs)``."""
        from .runner import Runner

        return Runner.stream(self, input, **kwargs)
