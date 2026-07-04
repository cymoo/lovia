"""Result and handle types returned by runner entry points."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .. import events
from ..agent import Agent
from ..approvals import ApprovalChannel
from ..messages import Message, Usage
from ..reliability import CancelToken
from ..transcript import TranscriptEntry, entries_to_messages


@dataclass
class RunResult:
    """The terminal state of a completed run.

    ``entries`` is **this run's own** transcript: the run's input plus everything
    it produced (assistant / reasoning / tool entries), across handoffs. It
    deliberately excludes the system prompt and prior session history, so it is
    the same whether the run finished fresh or was rebuilt from a checkpoint
    snapshot. For the *full* transcript (system + prior history + this run), read
    ``RunContext.entries`` inside a hook, or ``Session.load()`` after the run.

    ``messages`` is a derived, lossy chat-format view of ``entries`` (so it,
    too, is this-run-only and does not lead with the system message).

    ``finish_reason`` is the provider-reported finish reason of the run's
    final model turn (``"stop"``, ``"length"``, ...) — check it to tell a
    complete answer from a ``max_tokens``-truncated one. ``None`` when the
    provider reported none or the result was replayed from a completed
    checkpoint (it is not persisted in snapshots).
    """

    output: Any
    entries: list[TranscriptEntry]
    final_agent: Agent[Any]
    usage: Usage
    turns: int
    finish_reason: str | None = None

    @property
    def messages(self) -> list[Message]:
        """Lossy message view derived from :attr:`entries`."""
        return entries_to_messages(self.entries)

    def __repr__(self) -> str:
        """Compact summary — the full ``entries`` list is too noisy to dump.

        Shows the output (truncated), turn count, and token totals so an
        interactive ``print(result)`` is informative without flooding the REPL.
        """
        # ``repr`` of the output handles both cases cleanly: a str renders
        # quoted ('hi'), a model/dataclass renders as its own repr (Brief(...)).
        shown = repr(self.output)
        if len(shown) > 80:
            shown = shown[:77] + "..."
        return (
            f"RunResult(output={shown}, agent={self.final_agent.name!r}, "
            f"turns={self.turns}, tokens={self.usage.total_tokens})"
        )


class RunHandle:
    """Awaitable, async-iterable handle to a streamed run.

    Iteration is single-shot and **never raises for run failures**: every
    stream ends with exactly one terminal event —
    :class:`~lovia.events.RunCompleted` or :class:`~lovia.events.RunFailed` —
    and then stops. :meth:`result` returns the :class:`RunResult`, or raises
    the failure (:class:`~lovia.exceptions.RunCancelled`,
    :class:`~lovia.exceptions.BudgetExceeded`, ...), so the ``async for`` body
    stays free of try/except. Only ``asyncio`` task cancellation and other
    ``BaseException``\\ s still propagate through iteration.

    :meth:`cancel` requests cooperative cancellation without needing a
    pre-wired :class:`~lovia.CancelToken`.
    """

    def __init__(
        self,
        _stream: AsyncIterator[events.Event],
        approvals: ApprovalChannel,
        cancel_token: CancelToken,
    ) -> None:
        self._stream = _stream
        self._result: RunResult | None = None
        self._error: BaseException | None = None
        self._done = asyncio.Event()
        self._consumed = False
        self._cancel_token = cancel_token
        self.approvals = approvals

    def cancel(self, reason: str | None = None) -> None:
        """Request cooperative cancellation of this run.

        Same effect as cancelling the run's :class:`~lovia.CancelToken`: the
        loop stops at the next safe point, the stream ends with
        :class:`~lovia.events.RunFailed`, and :meth:`result` raises
        :class:`~lovia.exceptions.RunCancelled`.
        """
        self._cancel_token.cancel(reason)

    def __aiter__(self) -> AsyncIterator[events.Event]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[events.Event]:
        if self._consumed:
            raise RuntimeError("RunHandle can only be iterated once")
        self._consumed = True
        try:
            async for ev in self._stream:
                if isinstance(ev, events.RunCompleted):
                    self._result = ev.result
                yield ev
        except GeneratorExit:
            # The consumer broke out of iteration. Don't record this as the
            # run's error — a later ``result()`` call should report
            # abandonment, not re-raise GeneratorExit.
            self._done.set()
            raise
        except Exception as exc:
            # Terminal run failure. The loop already emitted RunFailed and
            # persisted terminal state; end iteration cleanly and hold the
            # exception for result().
            self._error = exc
            self._done.set()
        except BaseException as exc:
            # asyncio.CancelledError, KeyboardInterrupt, ...: not a run
            # outcome — record for result() but let it propagate.
            self._error = exc
            self._done.set()
            raise
        else:
            self._done.set()

    async def result(self) -> RunResult:
        """Return the final :class:`RunResult`, driving the stream if needed."""
        if not self._consumed:
            async for _ in self:
                pass
        else:
            await self._done.wait()
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise RuntimeError(
                "Run was abandoned before completion (the event stream was "
                "closed before RunCompleted was emitted)"
            )
        return self._result

    def __await__(self):  # type: ignore[no-untyped-def]
        return self.result().__await__()


__all__ = ["RunHandle", "RunResult"]
