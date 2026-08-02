"""Bridge between ``ask_human`` questions and ``POST /api/chat/answer``.

The mirror image of :mod:`lovia.web.approvals`, for the tool that asks the
*other* direction: the model requests operator input mid-run, the question
travels to the client as the ``ask_human`` ``tool_call`` event it already
streams (no extra event kind), and the browser answers over HTTP.

One registry serves every session: ``ask_human`` is an execution barrier
(``parallel=False``) and the web app runs at most one live run per session,
so "the pending question of session X" is unambiguous — the registry keys by
``HumanQuestion.session_id`` and ``POST /api/chat/answer`` needs only
``{session_id, answer}``.

Questions parked with nobody watching are the same hazard as approvals: a
clientless (scheduled) run would hold its concurrency slot forever. The same
remedy applies — ``timeout`` cancels the question after N seconds, the tool
call fails with a ToolError the model can route around, and the run moves on.
"""

from __future__ import annotations

import asyncio
import logging

from ..tools.human import HumanChannel, HumanQuestion

log = logging.getLogger(__name__)


class QuestionRegistry:
    """Session-scoped adapter around :class:`lovia.tools.HumanChannel`.

    :meth:`start` spawns the single consumer of ``channel.questions()`` —
    nothing else may iterate it. :meth:`aclose` closes the channel (cancelling
    any parked tool calls so runs wind down) and stops the consumer.
    """

    def __init__(self, channel: HumanChannel, *, timeout: float | None = None) -> None:
        self._channel = channel
        self._timeout = timeout
        self._by_session: dict[str, HumanQuestion] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        """Begin consuming questions. Requires a running event loop."""
        if self._task is None:
            self._task = asyncio.create_task(
                self._consume(), name="lovia-web-questions"
            )

    async def aclose(self) -> None:
        """Close the channel (failing parked calls) and stop the consumer."""
        self._channel.close("server shutting down")
        # Join the consumer FIRST (close() ends its iterator, so this is a
        # join, not a kill): a question it already pulled from the feed would
        # otherwise repopulate the maps and arm a timer after the clear.
        if self._task is not None:
            await self._task
            self._task = None
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        self._by_session.clear()

    async def _consume(self) -> None:
        async for q in self._channel.questions():
            sid = q.session_id or ""
            # One live run per session and ask_human is a barrier, so a
            # previous entry can only be a resolved leftover — replace it.
            self._drop_timer(sid)
            self._by_session[sid] = q
            if self._timeout is not None:
                loop = asyncio.get_running_loop()
                self._timers[sid] = loop.call_later(
                    self._timeout, self._expire, sid, q.id
                )
            log.info(
                "question parked: session=%s options=%d (timeout: %s)",
                sid or "<none>",
                len(q.options),
                "none — waits for an answer"
                if self._timeout is None
                else f"auto-cancel after {self._timeout:.0f}s",
            )

    # -- resolution -------------------------------------------------------- #
    def pending(self, session_id: str) -> HumanQuestion | None:
        """The session's pending question, if it is still unanswered."""
        q = self._by_session.get(session_id)
        if q is None:
            return None
        if all(p.id != q.id for p in self._channel.pending):
            # Resolved behind our back (run cancelled, channel closed): the
            # entry is stale — drop it rather than serve a ghost.
            self._forget(session_id)
            return None
        return q

    def resolve(self, session_id: str, answer: str) -> bool:
        """Answer the session's pending question. ``False`` if none matches."""
        q = self.pending(session_id)
        if q is None:
            return False
        self._forget(session_id)
        self._channel.answer(q.id, answer)
        return True

    def cancel_session(self, session_id: str, reason: str = "run cancelled") -> None:
        """Cancel the session's pending question(s), if any (idempotent)."""
        q = self._by_session.get(session_id)
        if q is not None:
            self._forget(session_id)
            self._channel.cancel(q.id, reason)
        # The consumer indexes asynchronously — a just-asked question may
        # still be in the feed only. Sweep the channel too, so a cancelled
        # run can never stay parked on a question we have not seen yet.
        for pending in self._channel.pending:
            if (pending.session_id or "") == session_id:
                self._channel.cancel(pending.id, reason)

    # -- internals --------------------------------------------------------- #
    def _expire(self, session_id: str, question_id: str) -> None:
        q = self._by_session.get(session_id)
        if q is None or q.id != question_id:  # answered or replaced meanwhile
            return
        self._forget(session_id)
        self._channel.cancel(
            question_id,
            f"no answer within {self._timeout:.0f}s — continue without one",
        )
        log.info("question for session %s timed out: cancelled", session_id or "<none>")

    def _forget(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)
        self._drop_timer(session_id)

    def _drop_timer(self, session_id: str) -> None:
        timer = self._timers.pop(session_id, None)
        if timer is not None:
            timer.cancel()


__all__ = ["QuestionRegistry"]
