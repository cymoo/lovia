"""Unit tests for ``lovia.runtime.result`` — the streamed run handle.

Focuses on the lifecycle edges: single-shot iteration, the terminal-event
contract (iteration never raises for run failures; ``result()`` does), the
two abandonment paths (consumer breaks out mid-stream, or the stream ends
without ``RunCompleted``), and ``cancel()`` delegation.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from lovia import Agent, CancelToken, events
from lovia.approvals import ApprovalChannel
from lovia.messages import Usage
from lovia.runtime.result import RunHandle, RunResult


def _run_result() -> RunResult:
    return RunResult(
        output="done",
        entries=[],
        final_agent=Agent(name="a"),
        usage=Usage(),
        turns=1,
    )


async def _stream_completes() -> AsyncIterator[events.Event]:
    yield events.RunCompleted(result=_run_result())


async def _stream_no_completion() -> AsyncIterator[events.Event]:
    yield events.TextDelta(delta="hi")
    # ends without ever emitting RunCompleted


async def _stream_raises() -> AsyncIterator[events.Event]:
    # Mirrors the real loop: RunFailed is emitted, then the generator raises.
    yield events.TextDelta(delta="partial")
    yield events.RunFailed(error=RuntimeError("provider exploded"))
    raise RuntimeError("provider exploded")


def _handle(stream: AsyncIterator[events.Event]) -> RunHandle:
    return RunHandle(stream, ApprovalChannel(), CancelToken())


async def test_result_returns_run_result() -> None:
    handle = _handle(_stream_completes())
    result = await handle.result()
    assert result.output == "done"


async def test_await_drives_the_stream() -> None:
    # __await__ delegates to result(); awaiting the handle directly works.
    result = await _handle(_stream_completes())
    assert result.output == "done"


async def test_iteration_is_single_shot() -> None:
    handle = _handle(_stream_completes())
    async for _ in handle:
        pass
    with pytest.raises(RuntimeError, match="only be iterated once"):
        async for _ in handle:
            pass


async def test_result_reraises_stream_error() -> None:
    handle = _handle(_stream_raises())
    with pytest.raises(RuntimeError, match="provider exploded"):
        await handle.result()


async def test_iteration_ends_cleanly_on_failure() -> None:
    # The terminal contract: a failing run closes the stream with RunFailed;
    # iteration ends without raising, and result() re-raises the error.
    handle = _handle(_stream_raises())
    seen: list[events.Event] = []
    async for ev in handle:
        seen.append(ev)
    assert isinstance(seen[-1], events.RunFailed)
    with pytest.raises(RuntimeError, match="provider exploded"):
        await handle.result()


async def test_task_cancellation_still_propagates() -> None:
    # asyncio-level cancellation is not a run outcome: it must escape the
    # iteration rather than be swallowed into the terminal-event contract.
    started = asyncio.Event()

    async def _stream_blocks() -> AsyncIterator[events.Event]:
        yield events.TextDelta(delta="hi")
        started.set()
        await asyncio.Event().wait()  # block until cancelled

    async def consume() -> None:
        async for _ in _handle(_stream_blocks()):
            pass

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_result_raises_when_abandoned_without_completion() -> None:
    handle = _handle(_stream_no_completion())
    with pytest.raises(RuntimeError, match="abandoned before completion"):
        await handle.result()


async def test_result_reports_abandonment_after_consumer_breaks_out() -> None:
    handle = _handle(_stream_no_completion())
    async for ev in handle:
        assert isinstance(ev, events.TextDelta)
        break  # consumer bails -> GeneratorExit into _iter
    # The break must surface as abandonment, not re-raise GeneratorExit.
    with pytest.raises(RuntimeError, match="abandoned before completion"):
        await handle.result()


async def test_cancel_delegates_to_token() -> None:
    token = CancelToken()
    handle = RunHandle(_stream_completes(), ApprovalChannel(), token)
    handle.cancel("user clicked stop")
    assert token.is_cancelled
