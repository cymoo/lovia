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
from typing import TYPE_CHECKING, Generic, TypeVar

from .messages import ChatMessage, Usage

if TYPE_CHECKING:
    from .agent import Agent


TContext = TypeVar("TContext")


@dataclass
class RunContext(Generic[TContext]):
    """State shared across a single run.

    Attributes:
        context: User-supplied dependency object (whatever was passed via
            ``Runner.run(..., context=...)``). ``None`` when not supplied.
        messages: Live, mutable transcript. Mutating it from a tool affects
            subsequent model turns — usually you want to read, not write.
        agent: The currently active agent (changes across handoffs).
        usage: Cumulative token usage for this run.
    """

    context: TContext | None
    messages: list[ChatMessage]
    agent: "Agent"
    usage: Usage = field(default_factory=Usage)
