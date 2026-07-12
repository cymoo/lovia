"""Agent definition.

An :class:`Agent` is a **declarative** specification of an LLM-driven actor.
It contains no runtime state: every call to :func:`Runner.run` reads from it
but never mutates it. This makes agents safe to share across requests and
trivial to clone for per-request tweaks.

The one exception to "no runtime state" is the ``_fragments`` tuple populated
by the :meth:`instruction` decorator — but it's still configuration, not
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

from .context.compaction import Compaction
from .exceptions import UserError
from .handoff import Handoff, agent_as_tool
from .providers import ModelSettings, Provider, provider_from_string
from .reliability import RetryPolicy
from .run_context import RunContext
from .tools import Tool

if TYPE_CHECKING:
    from .context.policy import ContextPolicy
    from .guardrails import GuardrailFn
    from .hooks import AgentHooks
    from .messages import Message, ToolCall
    from .reliability import RunBudget
    from .output import OutputRepairStrategy
    from .plugins import Plugin
    from .runtime.result import RunHandle, RunResult
    from .tools import ToolResultRenderer
    from .workspace.protocol import WorkspaceLike


TContext = TypeVar("TContext")


# An instructions callable receives the run's ``RunContext`` (the same handle
# tools, hooks, and guardrails get — reach the user deps via ``ctx.deps``) and
# returns a system-prompt fragment. May be sync or async.
InstructionsFn = Callable[["RunContext[Any]"], "str | Awaitable[str]"]

# A programmatic approval decision: ``True``/``"allow"`` permits the call,
# ``False``/``"deny"`` blocks it, and ``"ask"`` defers to the streaming
# consumer (the call is denied if nobody resolves it — fail closed).
ApprovalDecision = Union[bool, Literal["allow", "deny", "ask"]]

# A programmatic approval handler consulted for tools gated by
# ``needs_approval``. May be sync or async (the runner awaits the result
# either way).
ApprovalHandler = Callable[
    ["ToolCall", "RunContext[Any]"],
    "ApprovalDecision | Awaitable[ApprovalDecision]",
]


@dataclass
class Agent(Generic[TContext]):
    """Declarative description of an agent.

    The generic parameter ``TContext`` is the type of the optional dependency
    object passed to :meth:`Runner.run` as ``context=...``. Tools annotated
    with ``RunContext[TContext]`` receive a typed context handle.

    Two conventions hold across every field (each field documents its own
    specifics — hover it in your editor):

    * **Posture vs limits.** Posture — how the agent behaves when
      infrastructure hiccups (``retry``, ``default_tool_retries`` /
      ``default_tool_timeout``, ``context_policy``) — lives here and is
      inherited by every run. Limits —
      how much one request may spend (``max_turns``, ``budget``,
      cancellation) — are :meth:`Runner.run` arguments.
    * **Defaults are literal.** ``None`` never hides a constant: it means
      off, inherit, or auto-created, per the field's doc. Concrete defaults
      appear in the field definition itself.
    """

    name: str
    """Human-readable agent name; also used to derive handoff tool names."""

    instructions: "str | InstructionsFn" = ""
    """The base system prompt: a static string, or a callable receiving the
    run's :class:`RunContext` (reach user deps via ``ctx.deps``) and returning
    one, sync or async. Additional dynamic fragments can be registered with
    the :meth:`instruction` decorator and are appended at render time."""

    model: "str | Provider | None" = None
    """A ``"vendor:model"`` string (e.g. ``"openai:gpt-5.5"``) or a pre-built
    :class:`Provider` instance. Required before the agent can run: there is
    deliberately no default vendor, so an unconfigured agent raises
    :class:`UserError` instead of silently calling one. For multi-vendor
    failover point ``base_url`` at a routing gateway (LiteLLM, OpenRouter);
    transient errors are handled by ``retry``. See also
    :func:`lovia.model_from_env`."""

    tools: list[Tool] = field(default_factory=list)
    """Tools the agent may call."""

    output_type: Any = str
    """Pydantic model, dataclass, TypedDict, or builtin type describing the
    structured final output. ``str`` (the default) means free-form text.
    :meth:`Runner.run` may override this per call."""

    output_repair: "bool | OutputRepairStrategy" = True
    """When ``True`` (default), a failed structured-output parse triggers one
    repair prompt before giving up. ``False`` fails fast with
    :class:`OutputValidationError`; an
    :class:`~lovia.output.OutputRepairStrategy` instance customizes the retry
    policy (multi-attempt, localised prompts, ...)."""

    handoffs: list["Agent[Any] | Handoff"] = field(default_factory=list)
    """Agents (or :class:`Handoff` wrappers) the model may transfer control to
    via a synthetic ``transfer_to_<name>`` tool."""

    settings: ModelSettings = field(default_factory=ModelSettings)
    """Sampling parameters forwarded to the provider."""

    retry: "RetryPolicy | None" = field(default_factory=RetryPolicy)
    """Provider retry posture applied to every run of this agent. The default
    :class:`~lovia.RetryPolicy` retries transient errors (4 retries, jittered
    backoff); ``None`` disables provider retries. :meth:`Runner.run` may
    override it per call."""

    context_policy: "ContextPolicy" = field(default_factory=Compaction)
    """How this agent's context is shaped for each model call. The default
    :class:`~lovia.Compaction` sizes itself to the provider's advertised
    context window at call time and falls back to reactive overflow handling
    when the window is unknown. Per-call override via :meth:`Runner.run`
    wins; pass ``NoopContextPolicy()`` to disable compaction."""

    workspace: "WorkspaceLike | None" = None
    """Optional :class:`~lovia.workspace.Workspace` (or any ``WorkspaceLike``)
    scoping file/shell tools to a directory and permission policy. Its tool
    bundle is merged at run time and its live session is injected into
    ``RunContext.workspace``. ``None`` = no filesystem/shell tools."""

    plugins: list["Plugin"] = field(default_factory=list)
    """Declarative features bundling tools, per-turn view injectors, static
    system-prompt text, hooks, and guardrails. Each is activated once per run
    (and per agent on a handoff). See :mod:`lovia.plugins`."""

    hooks: "AgentHooks | None" = None
    """Optional :class:`AgentHooks` whose handlers receive every run event.
    ``None`` = no observers."""

    approval_handler: ApprovalHandler | None = None
    """Programmatic policy consulted when a tool gated by ``needs_approval``
    is about to run: return ``True``/``"allow"`` to permit, ``False``/
    ``"deny"`` to block, ``"ask"`` to defer to the streaming consumer. With
    ``None``, the streaming consumer decides — and an unresolved request is
    denied, so runs never hang."""

    input_guardrails: list["GuardrailFn"] = field(default_factory=list)
    """Checks run against the input before the first model call; returning a
    reason string (or ``True``) aborts with :class:`GuardrailTripped`."""

    output_guardrails: list["GuardrailFn"] = field(default_factory=list)
    """Checks run against the final output before it is returned; same
    contract as ``input_guardrails``."""

    default_tool_retries: int = 0
    """Retries applied to every tool whose own ``retries`` is ``None``.
    Per-tool ``@tool(retries=...)`` overrides."""

    default_tool_timeout: float | None = None
    """Per-attempt timeout (seconds) for every tool whose own ``timeout`` is
    ``None``. ``None`` = no timeout. Per-tool ``@tool(timeout=...)``
    overrides."""

    max_tool_output_chars: int | None = 200_000
    """Cap on the rendered tool-output string stored in the transcript, in
    characters. Anything longer is truncated (head + tail kept, with a
    marker) before it enters the transcript, and the raw return value is
    dropped — bounding memory, checkpoint, and session cost. Lossy: the cut
    middle is gone (``recall_tool_result`` sees the truncated version too);
    tools needing full-fidelity recovery should write to the workspace. The
    default (200 000 chars ≈ 50K tokens) is a tripwire, not a policy — far
    above any legitimate single result, it only catches runaway payloads.
    Per-tool ``max_output_chars`` overrides it; ``None`` stores outputs in
    full."""

    tool_result_renderer: "ToolResultRenderer | None" = None
    """Agent-wide renderer applied to any tool whose own ``result_renderer``
    is ``None`` (e.g. always JSON-serialize via a custom encoder). Successful
    results only: runner-produced ``"Tool error: ..."`` strings bypass
    renderers. ``None`` = the default rendering."""

    # Dynamic instruction fragments registered via @agent.instruction.
    # Rendered in registration order and appended after ``instructions``.
    # Not a public field — use the decorator or ``with_instructions`` instead.
    _fragments: tuple[InstructionsFn, ...] = field(
        default_factory=tuple, repr=False, compare=False
    )

    def resolve_provider(self) -> Provider:
        """Resolve ``model`` into a :class:`Provider` instance."""
        if self.model is None:
            raise UserError(
                f"Agent {self.name!r} has no model configured",
                hint='pass model="vendor:model" (e.g. "openai:gpt-5.5") '
                "or a Provider instance",
            )
        if isinstance(self.model, list):
            raise UserError(
                f"Agent {self.name!r}: model no longer accepts a list",
                hint="pass a single model; for multi-vendor failover point "
                "base_url at a routing gateway (LiteLLM, OpenRouter), and rely "
                "on retry=RetryPolicy(...) for transient errors",
            )
        if isinstance(self.model, str):
            return provider_from_string(self.model)
        return self.model

    # ------------------------------------------------------------------ #
    # Dynamic system-prompt fragments
    # ------------------------------------------------------------------ #

    def instruction(self, fn: InstructionsFn) -> InstructionsFn:
        """Register a dynamic instructions fragment.

        Registration mutates this agent in place — the one deliberate
        exception to the "immutable by convention" rule, kept for decorator
        ergonomics. The boundary with :meth:`clone` is copy-on-register:
        fragments registered *before* a clone are carried into it; fragments
        registered *after* affect only the agent they were registered on.
        Register fragments at definition time (right after constructing the
        agent), or use :meth:`with_instructions` for a purely functional
        variant.

        The decorated callable receives the run context and returns (or
        awaits) a string. All fragments are concatenated to ``instructions``
        at render time, in registration order, separated by blank lines —
        together they make up the rendered system prompt the model sees
        (observable afterwards as :attr:`RunContext.system_prompt`). Returning
        an empty string skips the fragment.

        Example::

            agent = Agent(name="x", instructions="You are helpful.")

            @agent.instruction
            async def add_user_tier(ctx) -> str:
                return f"User tier: {ctx.deps.tier}"
        """
        self._fragments = (*self._fragments, fn)
        return fn

    def with_instructions(self, fn: InstructionsFn) -> "Agent[TContext]":
        """Return a clone with one additional dynamic instructions fragment."""
        return self.clone(_fragments=(*self._fragments, fn))

    async def render_system_prompt(
        self, ctx: "RunContext[Any]", *, extra: "str | InstructionsFn | None" = None
    ) -> str:
        """Materialize the system prompt for a given run.

        Concatenates: the base ``instructions`` (str or callable), every
        fragment registered via :meth:`instruction`, and finally ``extra``
        (a per-call addendum supplied by the runner). Every callable receives
        ``ctx`` (a :class:`RunContext` — read user deps via ``ctx.deps``), the
        same handle tools and hooks get. Empty results are skipped so users can
        return ``""`` to opt out conditionally. The returned string is what
        surfaces as :attr:`RunContext.system_prompt`.
        """
        parts: list[str] = []

        async def render(fragment: "str | InstructionsFn | None") -> str:
            if fragment is None:
                return ""
            if callable(fragment):
                result = fragment(ctx)
                if hasattr(result, "__await__"):
                    return str(await result)
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
        max_turns: int = 50,
        budget: "RunBudget | None" = None,
        retry: "RetryPolicy | None" = None,
        context_policy: "ContextPolicy | None" = None,
    ) -> Tool:
        """Expose this agent as a :class:`Tool` callable by other agents.

        The execution-policy keywords are forwarded to the sub-run; see
        :func:`~lovia.handoff.agent_as_tool`.
        """
        return agent_as_tool(
            self,
            name=name,
            description=description,
            max_turns=max_turns,
            budget=budget,
            retry=retry,
            context_policy=context_policy,
        )

    def clone(self, **overrides: Any) -> "Agent[TContext]":
        """Return a copy of this agent with selected fields overridden.

        Dynamic system-prompt fragments registered on the source agent are
        carried over as an immutable tuple, so the clone inherits them without
        sharing mutable prompt state.
        """
        return replace(self, **overrides)

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
