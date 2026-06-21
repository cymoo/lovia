"""Unit tests for ``lovia.runtime.result`` — the streamed run handle.

Focuses on the lifecycle edges: single-shot iteration, error propagation
through :meth:`RunHandle.result`, and the two abandonment paths (consumer
breaks out mid-stream, or the stream ends without ``RunCompleted``).
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from lovia import Agent, events
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
    yield events.TextDelta(delta="partial")
    raise RuntimeError("provider exploded")


def _handle(stream: AsyncIterator[events.Event]) -> RunHandle:
    return RunHandle(stream, ApprovalChannel())


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


async def test_result_reraises_recorded_error_after_iteration() -> None:
    # When the error was recorded during a prior iteration, a later result()
    # call must re-raise the same error rather than report abandonment.
    handle = _handle(_stream_raises())
    with pytest.raises(RuntimeError, match="provider exploded"):
        async for _ in handle:
            pass
    with pytest.raises(RuntimeError, match="provider exploded"):
        await handle.result()


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
