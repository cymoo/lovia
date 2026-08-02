"""``ask_human`` — pause and request input from a human operator.

Mirrors the design of :class:`lovia.approvals.ApprovalChannel`: when the
model invokes the tool, the runner emits a future that any external code
(UI, CLI prompt, Slack bot, ...) can resolve via the
:class:`HumanChannel`. The idiomatic consumer is an ``async for`` over
:meth:`HumanChannel.questions`::

    from lovia.tools.human import HumanChannel, ask_human

    channel = HumanChannel()
    agent = Agent(name="x", tools=[ask_human(channel)])

    async def operator() -> None:
        async for q in channel.questions():   # ends when channel.close()
            channel.answer(q.id, await get_reply_somehow(q.question))

Tool calls block until an answer is supplied or the channel is closed.

A question may carry :attr:`~HumanQuestion.options` — 2–4 choices the model
offers when the question is a pick-one (or, with ``multi_select``, pick-many).
Options are *suggestions for the UI*: an answer is always a free-form string,
so a consumer renders them as buttons but keeps a text input as the escape
hatch. For a multi-select, join the chosen labels with ``", "``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, AsyncIterator, Sequence

from pydantic import BaseModel, Field

from ..exceptions import ToolError
from ..run_context import RunContext
from .base import Tool, tool

__all__ = ["HumanChannel", "HumanQuestion", "QuestionOption", "ask_human"]


class QuestionOption(BaseModel):
    """One selectable choice offered alongside a question."""

    label: str = Field(
        description=(
            "Short display label (1-5 words). Choosing the option answers "
            "with this exact text."
        )
    )
    description: str = Field(
        default="",
        description=(
            "Optional one-line explanation of the choice — its meaning or "
            "trade-off. Empty when the label alone is clear."
        ),
    )


@dataclass
class HumanQuestion:
    id: str
    question: str
    options: list[QuestionOption] = field(default_factory=list)
    """Choices to offer; empty for a free-form question."""
    multi_select: bool = False
    """Whether several options may be picked together."""
    session_id: str | None = None
    """``RunContext.session_id`` of the asking run, for consumers that route
    questions across sessions (``None`` for one-shot runs)."""
    run_id: str | None = None
    """``RunContext.run_id`` of the asking run (``None`` without a checkpoint)."""


@dataclass
class HumanChannel:
    """Out-of-band channel for resolving ``ask_human`` calls.

    Consume questions with ``async for q in channel.questions()`` (ends when
    :meth:`close` is called), or poll :attr:`pending`. Resolve with
    :meth:`answer` / :meth:`cancel` — or :meth:`close`, for all at once.

    Not thread-safe: like :class:`~lovia.approvals.ApprovalChannel`, resolving
    touches an :class:`asyncio.Future`, so calls must come from the event-loop
    thread. From another thread, hop over first::

        loop.call_soon_threadsafe(channel.answer, question_id, text)
    """

    _futures: dict[str, asyncio.Future[str]] = field(default_factory=dict)
    _pending: dict[str, HumanQuestion] = field(default_factory=dict)
    # New questions land here for questions(); ``None`` is the close sentinel.
    _feed: "asyncio.Queue[HumanQuestion | None]" = field(default_factory=asyncio.Queue)
    _closed: bool = field(default=False)

    @property
    def pending(self) -> list[HumanQuestion]:
        return list(self._pending.values())

    async def questions(self) -> AsyncIterator[HumanQuestion]:
        """Yield each question as the model asks it, until :meth:`close`.

        Questions asked before iteration starts are queued and delivered
        first. Single consumer: two concurrent iterations would split the
        feed between them. A question already resolved (answered, cancelled,
        or timed out) while queued is skipped. Started on an already-closed
        channel, the iterator ends immediately.
        """
        while True:
            # After close() nothing is ever enqueued again, so awaiting an
            # empty feed would hang forever. Drain any backlog (close() only
            # enqueues one sentinel; an iterator started later must not wait
            # for a second one), then end.
            if self._closed and self._feed.empty():
                return
            q = await self._feed.get()
            if q is None:
                return
            if q.id in self._futures:  # still unresolved?
                yield q

    def _new_question(
        self,
        question: str,
        *,
        options: Sequence[QuestionOption] = (),
        multi_select: bool = False,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[HumanQuestion, asyncio.Future[str]]:
        if self._closed:
            raise ToolError("human channel is closed", tool_name="ask_human")
        q = HumanQuestion(
            id=str(uuid.uuid4()),
            question=question,
            options=list(options),
            multi_select=multi_select,
            session_id=session_id,
            run_id=run_id,
        )
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._futures[q.id] = fut
        self._pending[q.id] = q
        self._feed.put_nowait(q)
        return q, fut

    def _discard(self, question_id: str) -> None:
        self._futures.pop(question_id, None)
        self._pending.pop(question_id, None)

    def answer(self, question_id: str, answer: str) -> None:
        fut = self._futures.pop(question_id, None)
        self._pending.pop(question_id, None)
        if fut is not None and not fut.done():
            fut.set_result(answer)

    def cancel(self, question_id: str, reason: str = "cancelled") -> None:
        fut = self._futures.pop(question_id, None)
        self._pending.pop(question_id, None)
        if fut is not None and not fut.done():
            fut.set_exception(ToolError(reason, tool_name="ask_human"))

    def close(self, reason: str = "channel closed") -> None:
        """Cancel every outstanding question and end :meth:`questions`.

        Each blocked ``ask_human`` call fails with a :class:`ToolError`, which
        the runner feeds back to the model as a tool-error result; further
        ``ask_human`` calls fail immediately. Idempotent.
        """
        self._closed = True
        for question_id in list(self._futures):
            self.cancel(question_id, reason)
        self._feed.put_nowait(None)


def ask_human(channel: HumanChannel, *, name: str = "ask_human") -> Tool:
    """Build an ``ask_human`` tool wired to ``channel``."""

    # parallel=False: one pending question per run at a time. Humans answer
    # dialogs sequentially, later questions often depend on earlier answers,
    # and a UI can then treat "the run's pending question" as unambiguous.
    @tool(name=name, parallel=False)
    async def _ask(
        ctx: RunContext[Any],
        question: Annotated[str, "The question to ask the human."],
        options: Annotated[
            list[QuestionOption] | None,
            Field(
                default=None,
                min_length=2,
                max_length=4,
                description=(
                    "When the question is a choice, offer 2-4 mutually "
                    "exclusive options, the recommended one first. The human "
                    "may still answer in free text, so never add an "
                    "'other'/'none' filler option. Omit for open questions."
                ),
            ),
        ] = None,
        multi_select: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Set true when several options may be picked together; "
                    "the reply then joins the chosen labels with ', '."
                ),
            ),
        ] = False,
    ) -> str:
        """Ask the human operator a question and wait for their reply.

        - Blocks until the human answers; a cancelled question surfaces as a
          tool error you can route around.
        - Prefer options for pick-one (or pick-many) questions; the reply may
          still be any free text.
        """
        q, fut = channel._new_question(
            question,
            options=options or (),
            multi_select=multi_select,
            session_id=ctx.session_id,
            run_id=ctx.run_id,
        )
        try:
            return await fut
        finally:
            # On answer/cancel this is a no-op; on external cancellation
            # (tool timeout, run cancelled) it drops the now-unanswerable
            # question so ``pending`` doesn't accumulate ghosts.
            channel._discard(q.id)

    return _ask
