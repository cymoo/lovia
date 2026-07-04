"""Per-run state passed to tools, guardrails, and hooks.

``RunContext`` is generic on the caller's optional dependency type. Tools opt
in to receiving the context by **type-annotating** their first parameter as
``RunContext[MyDeps]`` — the runner reads the annotation and injects the
context automatically. Naming the parameter ``ctx`` or ``context`` no longer
matters; only the type annotation does.

Example::

    @dataclass
    class Deps:
        db: Database

    @tool
    async def lookup(ctx: RunContext[Deps], user_id: int) -> str:
        return await ctx.context.db.fetch(user_id)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from .messages import Message, Usage
from .reliability import CancelToken, RunBudget
from .steering import Mailbox
from .tracing import Tracer
from .transcript import InputEntry, TranscriptEntry, entries_to_messages

if TYPE_CHECKING:
    from .agent import Agent
    from .workspace.protocol import WorkspaceSession


TContext = TypeVar("TContext")


@dataclass
class RunContext(Generic[TContext]):
    """State shared across a single run.

    Tools, guardrails, hooks, and instruction fragments all receive the same
    live instance; each field documents itself — hover it in your editor.
    """

    context: TContext | None
    """User-supplied dependency object (whatever was passed via
    ``Runner.run(..., context=...)``). ``None`` when not supplied. Also
    reachable as :attr:`deps` — the friendlier name for the same object."""

    entries: list[TranscriptEntry]
    """Live transcript log. Treat it as **read-only**: it is the canonical
    record the runner appends to and persists. Mutating it mid-run
    (especially removing a :class:`ToolCallEntry` without its paired result)
    can leave the transcript in a state the provider rejects. To add
    conversational context, return it from a tool or pass it as the next
    ``input`` instead."""

    agent: "Agent[Any]"
    """The currently active agent (changes across handoffs)."""

    usage: Usage = field(default_factory=Usage)
    """Cumulative token usage for this run."""

    session_id: str | None = None
    """Stable conversation key when ``session=`` was passed to
    :meth:`Runner.run`; ``None`` for one-shot runs. Tools that key
    per-session resources (caches, memory) read it here."""

    run_id: str | None = None
    """Per-run idempotency key when ``checkpoint=`` was passed to
    :meth:`Runner.run`; ``None`` for runs without a checkpoint. Unlike
    :attr:`session_id` it is unique to this single run — tools that key
    per-run resources (scratch files, locks) read it here."""

    turn: int = 0
    """1-based index of the model turn currently in flight (``0`` before the
    first turn starts). Lets a tool or hook tell which step of the loop it is
    running in."""

    budget: RunBudget | None = None
    """The run's :class:`~lovia.RunBudget`, when one was passed to
    :meth:`Runner.run`; ``None`` if unconstrained. A tool can read its limits
    to self-throttle before expensive work; the runner still enforces it
    independently between turns."""

    workspace: "WorkspaceSession | None" = None
    """The active agent's live workspace session, when the agent has
    ``workspace=`` configured. The built-in file/shell tools read it here;
    custom tools may too. Swapped on handoff."""

    cancel_token: CancelToken = field(default_factory=CancelToken)
    """The run's cooperative cancellation signal. Always present (the runner
    creates one when the caller didn't pass it), so a tool or hook can call
    ``cancel()`` to request termination at the next safe point, and an
    agent-as-tool sub-run can inherit it."""

    mailbox: Mailbox = field(default_factory=Mailbox)
    """The run's inbound steering channel — the dual of :attr:`cancel_token`,
    and like it always present. A tool or hook can ``push()`` content to
    inject it as a ``user`` message at the next mailbox drain — each turn
    start, right after that turn's ``TurnStarted`` hooks fire, never
    mid-turn. A push during the run's final turn is not seen by this run
    (and, for a runner-created mailbox, by nobody — only a caller-supplied
    instance can be drained after the run). Agent-as-tool sub-runs get their
    own mailbox rather than inheriting this one; see
    :func:`~lovia.handoff.agent_as_tool`."""
    # The run's tracer (``None`` when untraced). Internal plumbing, not public
    # API — the same convention as ``ApprovalRequired._channel``: it exists so
    # agent-as-tool sub-runs inherit the parent's tracer and their spans join
    # the same trace.
    _tracer: Tracer | None = field(default=None, repr=False)

    @property
    def deps(self) -> TContext | None:
        """Alias for :attr:`context` — the user-supplied dependency object.

        ``ctx.deps.db`` reads more clearly than ``ctx.context.db`` once you
        have typed the run as ``RunContext[MyDeps]``. Both names point at the
        same object.
        """
        return self.context

    @property
    def messages(self) -> list[Message]:
        """Read-only chat-format view derived from :attr:`entries` on each access.

        A new list is returned every time; mutations to it are silently
        discarded. To modify the transcript, append to :attr:`entries` instead.
        """
        return entries_to_messages(self.entries)

    @property
    def system_prompt(self) -> str:
        """The fully rendered system prompt sent to the model this run.

        Returns the concatenation of the agent's ``instructions``, every
        dynamic ``@agent.instruction`` fragment, plugin instructions, and any
        structured-output / ``extra_instructions`` addendum — i.e. exactly the
        leading system text the provider saw. Empty string when the run has no
        system prompt. Handy for debugging "why did the model do that?" without
        re-deriving the prompt by hand.
        """
        first = self.entries[0] if self.entries else None
        if isinstance(first, InputEntry) and first.role == "system":
            content = first.content
            return content if isinstance(content, str) else ""
        return ""
