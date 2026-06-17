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

from .messages import Message, Usage
from .reliability import CancelToken
from .transcript import TranscriptEntry, entries_to_messages

if TYPE_CHECKING:
    from .agent import Agent
    from .workspace.protocol import WorkspaceSession


TContext = TypeVar("TContext")


@dataclass
class RunContext(Generic[TContext]):
    """State shared across a single run.

    Attributes:
        context: User-supplied dependency object (whatever was passed via
            ``Runner.run(..., context=...)``). ``None`` when not supplied.
        entries: Live transcript log. This is the canonical record — prefer
            reading over writing. Appending directly affects subsequent model
            turns.
        agent: The currently active agent (changes across handoffs).
        usage: Cumulative token usage for this run.
        session_id: Stable conversation key when ``session=`` was passed to
            :meth:`Runner.run`. ``None`` for one-shot runs. Tools that key
            per-session resources (caches, memory) read it here.
        workspace: The active agent's live workspace session, when the agent
            has ``workspace=`` configured. The built-in file/shell tools read
            it here; custom tools may too. Swapped on handoff.
        cancel_token: The run's cooperative cancellation signal. Always present
            (the runner creates one when the caller didn't pass it), so a tool
            or hook can call ``cancel()`` to request the run terminate at the
            next safe point, and an agent-as-tool sub-run can inherit it.
    """

    context: TContext | None
    entries: list[TranscriptEntry]
    agent: "Agent"
    usage: Usage = field(default_factory=Usage)
    session_id: str | None = None
    workspace: "WorkspaceSession | None" = None
    cancel_token: CancelToken = field(default_factory=CancelToken)

    @property
    def messages(self) -> list[Message]:
        """Read-only chat-format view derived from :attr:`entries` on each access.

        A new list is returned every time; mutations to it are silently
        discarded. To modify the transcript, append to :attr:`entries` instead.
        """
        return entries_to_messages(self.entries)
