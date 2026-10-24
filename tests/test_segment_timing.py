"""Each session segment records when its run ran (``Segment.meta["timing"]``)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from lovia import Agent, CheckpointOptions, InMemoryCheckpointer, Runner
from lovia.checkpointer import RunHead, RunSnapshot
from lovia.plugins.base import PluginInstance
from lovia.messages import Usage
from lovia.session import TIMING_META_KEY
from lovia.stores import InMemorySession
from lovia.transcript import AssistantTextEntry, InputEntry

from .scripted_provider import ScriptedProvider, text


async def test_a_finished_run_times_its_segment() -> None:
    session = InMemorySession()
    before = time.time()
    await Runner.run(
        Agent(name="a", model=ScriptedProvider([text("hi")])),
        "go",
        session=session,
        session_id="s",
    )
    (seg,) = await session.segments("s")
    timing = (seg.meta or {})[TIMING_META_KEY]
    assert before <= timing["started_at"] <= timing["finished_at"] <= time.time()


async def test_slow_setup_counts_toward_the_run() -> None:
    # A plugin that takes a while to set up (an MCP connect, say) is part of
    # what the user waited for.
    class SlowSetup:
        name = "slow"

        async def setup(self, ctx: Any) -> PluginInstance:
            await asyncio.sleep(0.2)
            return PluginInstance()

    session = InMemorySession()
    await Runner.run(
        Agent(name="a", model=ScriptedProvider([text("hi")]), plugins=[SlowSetup()]),
        "go",
        session=session,
        session_id="s",
    )
    (seg,) = await session.segments("s")
    timing = (seg.meta or {})[TIMING_META_KEY]
    assert timing["finished_at"] - timing["started_at"] >= 0.2


async def test_a_resumed_run_keeps_its_original_start() -> None:
    cp, session = InMemoryCheckpointer(), InMemorySession()
    snap = RunSnapshot(
        run_id="r1",
        agent_name="a",
        entries=[InputEntry(role="user", content="go")],
        usage=Usage(),
        turns=0,
        status="interrupted",
        started_at=1000.0,
    )
    await cp.append(snap.run_id, snap.entries, snap.head)
    await Runner.run(
        Agent(name="a", model=ScriptedProvider([text("hi")])),
        [],
        session=session,
        session_id="s",
        checkpoint=CheckpointOptions(cp, "r1"),
    )
    (seg,) = await session.segments("s")
    assert (seg.meta or {})[TIMING_META_KEY]["started_at"] == 1000.0


async def test_healing_a_completed_run_times_it_from_its_checkpoint() -> None:
    # A crash between the completion checkpoint and the session append: the
    # replay appends the segment, finished when the completion was saved.
    cp, session = InMemoryCheckpointer(), InMemorySession()
    snap = RunSnapshot(
        run_id="r1",
        agent_name="a",
        entries=[
            InputEntry(role="user", content="go"),
            AssistantTextEntry(content="done"),
        ],
        usage=Usage(),
        turns=1,
        status="completed",
        output="done",
        updated_at=1060.0,
        started_at=1000.0,
    )
    await cp.append(snap.run_id, snap.entries, snap.head)
    await Runner.run(
        Agent(name="a", model=ScriptedProvider([])),
        [],
        session=session,
        session_id="s",
        checkpoint=CheckpointOptions(cp, "r1"),
    )
    (seg,) = await session.segments("s")
    assert (seg.meta or {})[TIMING_META_KEY] == {
        "started_at": 1000.0,
        "finished_at": 1060.0,
    }


async def test_resuming_a_snapshot_from_before_timing_leaves_the_segment_untimed() -> (
    None
):
    cp, session = InMemoryCheckpointer(), InMemorySession()
    head = RunHead.from_dict(
        {"agent_name": "a", "status": "interrupted", "turns": 0}  # no started_at
    )
    assert head.started_at is None
    await cp.append("r1", [InputEntry(role="user", content="go")], head)
    await Runner.run(
        Agent(name="a", model=ScriptedProvider([text("hi")])),
        [],
        session=session,
        session_id="s",
        checkpoint=CheckpointOptions(cp, "r1"),
    )
    (seg,) = await session.segments("s")
    assert TIMING_META_KEY not in (seg.meta or {})


def test_run_head_round_trips_its_start() -> None:
    head = RunHead(agent_name="a", usage=Usage(), turns=1, started_at=1000.0)
    assert RunHead.from_json(head.to_json()).started_at == 1000.0
