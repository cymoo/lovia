"""Unit tests for ``lovia.runtime.checkpoint`` — snapshot persistence policy.

Covers the resumable-vs-final exception classifier, the error payload shape,
and the :class:`CheckpointWriter` no-op / delete-on-success / non-serializable
output / best-effort-terminal-save behaviours against an in-memory backend.
"""

from __future__ import annotations

import asyncio

import pytest

from lovia import Agent
from lovia.checkpointer import RunHead
from lovia.transcript import TranscriptEntry
from lovia.exceptions import (
    BudgetExceeded,
    MaxTurnsExceeded,
    ProviderError,
    RunCancelled,
)
from lovia.messages import Usage
from lovia.run_context import RunContext
from lovia.runtime.checkpoint import CheckpointWriter, error_payload
from lovia.runtime.run_state import ActiveAgent, RunState
from lovia.stores import InMemoryCheckpointer


def _state() -> RunState:
    agent = Agent(name="a")
    run_ctx = RunContext(context=None, entries=[], agent=agent, usage=Usage())
    active = ActiveAgent(
        agent=agent, providers=[], structured_output=None, tools_by_name={}
    )
    return RunState(run_ctx=run_ctx, active=active, turns=2)


# ----------------------------------------------------------------- classify


@pytest.mark.parametrize(
    "exc",
    [
        RunCancelled("x"),
        asyncio.CancelledError(),
        MaxTurnsExceeded("x"),
        BudgetExceeded("x"),
        TimeoutError(),
        ConnectionError(),
        ProviderError("x"),  # retryable defaults to None -> treated as resumable
    ],
)
def test_classify_resumable_as_interrupted(exc: BaseException) -> None:
    assert CheckpointWriter.classify(exc) == "interrupted"


@pytest.mark.parametrize(
    "exc",
    [
        ProviderError("x", retryable=False),
        ValueError("plain bug"),
    ],
)
def test_classify_final_as_failed(exc: BaseException) -> None:
    assert CheckpointWriter.classify(exc) == "failed"


def test_error_payload_shape() -> None:
    assert error_payload(ValueError("boom")) == {
        "type": "ValueError",
        "message": "boom",
    }


# ------------------------------------------------------------ CheckpointWriter


async def test_writer_is_noop_without_checkpointer() -> None:
    writer = CheckpointWriter(checkpointer=None, run_id=None)
    # All of these must be harmless no-ops.
    await writer.delete()
    await writer.save_running(_state())
    await writer.complete(_state(), "out")


async def test_complete_persists_serializable_output() -> None:
    cp = InMemoryCheckpointer()
    writer = CheckpointWriter(checkpointer=cp, run_id="r1")
    await writer.complete(_state(), {"answer": 42})
    snap = await cp.load("r1")
    assert snap is not None
    assert snap.status == "completed"
    assert snap.output == {"answer": 42}
    assert snap.error is None


async def test_complete_with_delete_on_success_removes_snapshot() -> None:
    cp = InMemoryCheckpointer()
    await cp.append("r1", [], RunHead(agent_name="a", usage=Usage(), turns=1))
    writer = CheckpointWriter(checkpointer=cp, run_id="r1", delete_on_success=True)
    await writer.complete(_state(), "out")
    assert await cp.load("r1") is None


async def test_complete_flags_non_serializable_output() -> None:
    cp = InMemoryCheckpointer()
    writer = CheckpointWriter(checkpointer=cp, run_id="r1")
    await writer.complete(_state(), {"a", "b"})  # a set is not JSON-safe
    snap = await cp.load("r1")
    assert snap is not None
    assert snap.status == "completed"
    assert snap.output is None
    assert snap.error is not None
    assert snap.error["type"] == "OutputNotSerializable"


async def test_save_terminal_swallows_backend_failure() -> None:
    class _BoomCheckpointer(InMemoryCheckpointer):
        async def append(
            self, run_id: str, entries: list[TranscriptEntry], head: RunHead
        ) -> None:
            raise RuntimeError("disk full")

    writer = CheckpointWriter(checkpointer=_BoomCheckpointer(), run_id="r1")
    # A snapshot failure must never mask the original run error -> no raise.
    await writer.save_terminal(_state(), ValueError("original failure"))


async def test_save_terminal_records_classified_status() -> None:
    cp = InMemoryCheckpointer()
    writer = CheckpointWriter(checkpointer=cp, run_id="r1")
    await writer.save_terminal(_state(), MaxTurnsExceeded("too many"))
    snap = await cp.load("r1")
    assert snap is not None
    assert snap.status == "interrupted"
    assert snap.error == {"type": "MaxTurnsExceeded", "message": "too many"}
