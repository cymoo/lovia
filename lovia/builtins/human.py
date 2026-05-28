"""``ask_human`` — pause and request input from a human operator.

Mirrors the design of :class:`lovia.approvals.ApprovalChannel`: when the
model invokes the tool, the runner emits a future that any external code
(UI, CLI prompt, Slack bot, ...) can resolve via the
:class:`HumanChannel`.

::

    from lovia.builtins.human import HumanChannel, ask_human

    channel = HumanChannel()
    agent = Agent(name="x", tools=[ask_human(channel)])

    # From your UI / driver loop:
    await channel.answer("question-id", "the answer")

Tool calls block until an answer is supplied or the channel is closed.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Annotated

from ..exceptions import ToolError
from ..tools import Tool, tool


@dataclass
class HumanQuestion:
    id: str
    question: str


@dataclass
class HumanChannel:
    """Out-of-band channel for resolving ``ask_human`` calls.

    Use :attr:`pending` to inspect outstanding questions and
    :meth:`answer` (or :meth:`cancel`) to resolve them.
    """

    _futures: dict[str, asyncio.Future[str]] = field(default_factory=dict)
    _pending: dict[str, HumanQuestion] = field(default_factory=dict)

    @property
    def pending(self) -> list[HumanQuestion]:
        return list(self._pending.values())

    def _new_question(self, question: str) -> tuple[HumanQuestion, asyncio.Future[str]]:
        q = HumanQuestion(id=str(uuid.uuid4()), question=question)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._futures[q.id] = fut
        self._pending[q.id] = q
        return q, fut

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


def ask_human(channel: HumanChannel, *, name: str = "ask_human") -> Tool:
    """Build an ``ask_human`` tool wired to ``channel``."""

    @tool(name=name)
    async def _ask(
        question: Annotated[str, "The question to ask the human."],
    ) -> str:
        """Block until a human answers the question via the channel."""
        _, fut = channel._new_question(question)
        return await fut

    return _ask
