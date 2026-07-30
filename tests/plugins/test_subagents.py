"""Tests for the Subagents plugin: spawn/wait/cancel, delivery, lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from lovia import Agent, InMemorySession, RunContext, Runner, Subagents
from lovia.exceptions import UserError
from lovia.plugins.subagents import SubagentReport
from lovia.tools import tool
from lovia.transcript import ModelDelta, ToolResultEntry, TranscriptEntry

from ..scripted_provider import ScriptedProvider, batch, call, text


class GatedProvider:
    """Blocks every model call until ``gate`` is set, then delegates."""

    name = "gated"
    model = None
    supports_json_schema = False

    def __init__(self, inner: ScriptedProvider, gate: asyncio.Event) -> None:
        self.inner = inner
        self.gate = gate

    async def stream(
        self, entries: list[TranscriptEntry], **kwargs: Any
    ) -> AsyncIterator[ModelDelta]:
        await self.gate.wait()
        async for delta in self.inner.stream(entries, **kwargs):
            yield delta


def _barrier_tool():
    """A parent tool that returns once the run's mailbox has a queued item —
    the deterministic way to hold a turn open until a child has delivered."""

    @tool
    async def barrier(ctx: RunContext[Any]) -> str:
        """Wait until a subagent report is queued."""

        async def _poll() -> None:
            while not ctx.mailbox:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_poll(), timeout=5)
        return "ok"

    return barrier


def _nap_tool(gate: asyncio.Event):
    @tool
    async def nap(ctx: RunContext[Any]) -> str:
        """Park until the test releases the gate."""
        await asyncio.wait_for(gate.wait(), timeout=5)
        return "napped"

    return nap


def _release_tool(gate: asyncio.Event):
    @tool
    async def release(ctx: RunContext[Any]) -> str:
        """Open the gate."""
        gate.set()
        return "released"

    return release


def _tool_result(entries: list[TranscriptEntry], call_id: str) -> str:
    for entry in entries:
        if isinstance(entry, ToolResultEntry) and entry.call_id == call_id:
            return entry.output
    raise AssertionError(f"no tool result for {call_id}")


def _child(script: list) -> Agent[Any]:
    return Agent(name="researcher", model=ScriptedProvider(script))


# --------------------------------------------------------------------------- #
# spawn + wait (same turn: the withdraw path)
# --------------------------------------------------------------------------- #


async def test_spawn_then_wait_collects_report_once() -> None:
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                batch(
                    ("spawn_subagent", {"prompt": "research tides"}, "c_spawn"),
                    ("wait_subagents", {}, "c_wait"),
                ),
                text("final"),
            ]
        ),
        plugins=[Subagents(_child([text("tides go in and out")]))],
    )
    result = await Runner.run(parent, "go")
    assert result.output == "final"
    spawn_out = _tool_result(result.entries, "c_spawn")
    assert "t1" in spawn_out and "researcher" in spawn_out
    wait_out = _tool_result(result.entries, "c_wait")
    assert "[subagent t1: done]" in wait_out
    assert "tides go in and out" in wait_out
    # Withdrawn from the mailbox: the report reaches the context exactly once.
    provider = parent.model
    assert isinstance(provider, ScriptedProvider)
    final_turn = provider.calls[1]
    assert not any(
        m.role == "user" and "[subagent" in (m.content or "") for m in final_turn
    )
    # Child usage (2 tokens) folded into the parent's total (2 turns x 2 + 2).
    assert result.usage.total_tokens == 6


async def test_mailbox_delivery_arrives_next_turn() -> None:
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                batch(
                    ("spawn_subagent", {"prompt": "research"}, "c_spawn"),
                    ("barrier", {}, "c_bar"),
                ),
                text("done"),
            ]
        ),
        tools=[_barrier_tool()],
        plugins=[Subagents(_child([text("findings!")]))],
    )
    result = await Runner.run(parent, "go")
    assert result.output == "done"
    provider = parent.model
    assert isinstance(provider, ScriptedProvider)
    # The drained report is a real user message on the next model call.
    seen = [
        m.content
        for m in provider.calls[1]
        if m.role == "user" and "[subagent t1: done]" in (m.content or "")
    ]
    assert len(seen) == 1
    assert "findings!" in seen[0]


async def test_wait_after_delivery_reports_already_delivered() -> None:
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                batch(
                    ("spawn_subagent", {"prompt": "research"}, "c_spawn"),
                    ("barrier", {}, "c_bar"),
                ),
                call("wait_subagents", {}, call_id="c_wait"),
                text("done"),
            ]
        ),
        tools=[_barrier_tool()],
        plugins=[Subagents(_child([text("findings!")]))],
    )
    result = await Runner.run(parent, "go")
    wait_out = _tool_result(result.entries, "c_wait")
    assert "already delivered" in wait_out
    assert "findings!" not in wait_out  # not repeated


# --------------------------------------------------------------------------- #
# wait edge cases
# --------------------------------------------------------------------------- #


async def test_wait_timeout_reports_still_running() -> None:
    gate = asyncio.Event()  # never set: child stays parked
    child = Agent(name="slow", model=GatedProvider(ScriptedProvider([text("x")]), gate))
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                call("spawn_subagent", {"prompt": "slow work"}, call_id="c_spawn"),
                call("wait_subagents", {"timeout_seconds": 0}, call_id="c_wait"),
                text("done"),
            ]
        ),
        plugins=[Subagents(child)],
    )
    result = await Runner.run(parent, "go")
    wait_out = _tool_result(result.entries, "c_wait")
    assert "Timed out" in wait_out and "t1" in wait_out


async def test_wait_without_spawns_and_unknown_ids() -> None:
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                call("wait_subagents", {}, call_id="c_w1"),
                call("spawn_subagent", {"prompt": "p"}, call_id="c_spawn"),
                call("wait_subagents", {"ids": ["zzz"]}, call_id="c_w2"),
                text("done"),
            ]
        ),
        plugins=[Subagents(_child([text("r")]))],
    )
    result = await Runner.run(parent, "go")
    assert "No subagents have been spawned" in _tool_result(result.entries, "c_w1")
    assert "Unknown subagent id(s) zzz" in _tool_result(result.entries, "c_w2")


# --------------------------------------------------------------------------- #
# capacity / cancel / bounded teardown
# --------------------------------------------------------------------------- #


async def test_spawn_declines_at_capacity() -> None:
    gate = asyncio.Event()
    child = Agent(name="slow", model=GatedProvider(ScriptedProvider([text("x")]), gate))
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                batch(
                    ("spawn_subagent", {"prompt": "one"}, "c_s1"),
                    ("spawn_subagent", {"prompt": "two"}, "c_s2"),
                ),
                text("done"),
            ]
        ),
        plugins=[Subagents(child, max_concurrent=1)],
    )
    result = await Runner.run(parent, "go")
    outputs = {_tool_result(result.entries, cid) for cid in ("c_s1", "c_s2")}
    assert any("started in the background" in o for o in outputs)
    assert any("At capacity" in o for o in outputs)


async def test_cancel_subagent_no_report() -> None:
    gate = asyncio.Event()
    child = Agent(
        name="napper",
        model=ScriptedProvider([call("nap", {}), text("never sent")]),
        tools=[_nap_tool(gate)],
    )
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                call("spawn_subagent", {"prompt": "nap"}, call_id="c_spawn"),
                batch(
                    ("cancel_subagent", {"id": "t1"}, "c_cancel"),
                    ("release", {}, "c_rel"),
                ),
                call("wait_subagents", {}, call_id="c_wait"),
                text("done"),
            ]
        ),
        tools=[_release_tool(gate)],
        plugins=[Subagents(child)],
    )
    result = await Runner.run(parent, "go")
    assert "asked to stop" in _tool_result(result.entries, "c_cancel")
    assert "t1 was cancelled; no report" in _tool_result(result.entries, "c_wait")


async def test_bounded_run_end_cancels_children() -> None:
    gate = asyncio.Event()  # never set
    inner = ScriptedProvider([text("x")])
    child = Agent(name="slow", model=GatedProvider(inner, gate))
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [call("spawn_subagent", {"prompt": "p"}, call_id="c_spawn"), text("bye")]
        ),
        plugins=[Subagents(child)],
    )
    before = asyncio.all_tasks()
    result = await Runner.run(parent, "go")
    assert result.output == "bye"
    await asyncio.sleep(0)
    leaked = asyncio.all_tasks() - before
    assert not leaked  # aclose cancelled and awaited the parked child
    assert inner.calls == []  # the child never got past the gate


# --------------------------------------------------------------------------- #
# failures and truncation
# --------------------------------------------------------------------------- #


async def test_child_failure_delivers_failure_report() -> None:
    child = Agent(
        name="doomed",
        # Calls a tool it does not have; with max_turns=1 the follow-up turn
        # is over budget -> terminal MaxTurnsExceeded.
        model=ScriptedProvider([call("missing_tool", {})]),
    )
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                batch(
                    ("spawn_subagent", {"prompt": "fail"}, "c_spawn"),
                    ("wait_subagents", {}, "c_wait"),
                ),
                text("done"),
            ]
        ),
        plugins=[Subagents(child, max_turns=1)],
    )
    result = await Runner.run(parent, "go")
    wait_out = _tool_result(result.entries, "c_wait")
    assert "[subagent t1: failed]" in wait_out


async def test_report_body_is_truncated() -> None:
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                batch(
                    ("spawn_subagent", {"prompt": "p"}, "c_spawn"),
                    ("wait_subagents", {}, "c_wait"),
                ),
                text("done"),
            ]
        ),
        plugins=[Subagents(_child([text("x" * 500)]), max_result_chars=100)],
    )
    result = await Runner.run(parent, "go")
    wait_out = _tool_result(result.entries, "c_wait")
    assert "chars truncated" in wait_out
    assert len(wait_out) < 400


# --------------------------------------------------------------------------- #
# catalog and self-clone
# --------------------------------------------------------------------------- #


async def test_unknown_agent_name_lists_available() -> None:
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                call(
                    "spawn_subagent",
                    {"agent": "zzz", "prompt": "p"},
                    call_id="c_spawn",
                ),
                text("done"),
            ]
        ),
        plugins=[Subagents([_child([]), Agent(name="coder", model=None)])],
    )
    result = await Runner.run(parent, "go")
    out = _tool_result(result.entries, "c_spawn")
    assert "unknown subagent 'zzz'" in out
    assert "coder" in out and "researcher" in out


async def test_duplicate_child_names_rejected() -> None:
    with pytest.raises(UserError, match="two child agents named"):
        Subagents([Agent(name="a", model=None), Agent(name="a", model=None)])


async def test_self_clone_mode_strips_plugins() -> None:
    provider = ScriptedProvider(
        [
            batch(
                ("spawn_subagent", {"prompt": "sub work"}, "c_spawn"),
                ("wait_subagents", {}, "c_wait"),
            ),
            text("child answer"),  # popped by the cloned child
            text("parent final"),
        ]
    )
    parent = Agent(name="bot", model=provider, plugins=[Subagents()])
    result = await Runner.run(parent, "go")
    assert result.output == "parent final"
    wait_out = _tool_result(result.entries, "c_wait")
    assert "agent=bot-sub" in wait_out
    assert "child answer" in wait_out
    # The clone lost the plugin: the child's model call (calls[1]) carries no
    # subagent instructions anywhere — with no plugins and no base
    # instructions there is not even a system message, just the prompt.
    child_call = provider.calls[1]
    assert child_call[0].content == "sub work"
    assert not any("Background subagents" in (m.content or "") for m in child_call)


# --------------------------------------------------------------------------- #
# per-turn status injection and instructions
# --------------------------------------------------------------------------- #


async def test_injector_shows_running_children_in_bounded_mode() -> None:
    gate = asyncio.Event()
    child = Agent(name="slow", model=GatedProvider(ScriptedProvider([text("x")]), gate))
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [call("spawn_subagent", {"prompt": "p"}, call_id="c_spawn"), text("bye")]
        ),
        plugins=[Subagents(child)],
    )
    await Runner.run(parent, "go")
    provider = parent.model
    assert isinstance(provider, ScriptedProvider)
    system = provider.calls[0][0].content or ""
    assert "## Background subagents" in system
    assert "never finish your reply" in system
    reminders = [
        m.content
        for m in provider.calls[1]
        if m.role == "user" and "Background subagents running" in (m.content or "")
    ]
    assert len(reminders) == 1
    assert "t1 slow" in reminders[0]
    assert "Do not finish your reply" in reminders[0]


# --------------------------------------------------------------------------- #
# detached mode
# --------------------------------------------------------------------------- #


async def test_detached_child_outlives_run_and_delivers() -> None:
    gate = asyncio.Event()
    child = Agent(
        name="slow", model=GatedProvider(ScriptedProvider([text("late news")]), gate)
    )
    reports: list[SubagentReport] = []
    delivered = asyncio.Event()

    async def deliver(report: SubagentReport) -> None:
        reports.append(report)
        delivered.set()

    plugin = Subagents(child, deliver=deliver)
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [call("spawn_subagent", {"prompt": "p"}, call_id="c_spawn"), text("bye")]
        ),
        plugins=[plugin],
    )
    result = await Runner.run(parent, "go", session=InMemorySession(), session_id="s1")
    assert result.output == "bye"
    assert not reports  # the child is still parked when the run ends
    assert len(plugin._detached) == 1
    gate.set()
    await asyncio.wait_for(delivered.wait(), timeout=5)
    (report,) = reports
    assert report.id == "t1"
    assert report.session_id == "s1"
    assert report.error is None
    assert "[subagent t1: done]" in report.text
    assert "late news" in report.text
    await asyncio.sleep(0)
    assert not plugin._detached  # done-callback dropped the strong ref
    # Detached instructions allow finishing while children run.
    provider = parent.model
    assert isinstance(provider, ScriptedProvider)
    system = provider.calls[0][0].content or ""
    assert "may finish your reply" in system


async def test_empty_prompt_spawns_nothing() -> None:
    parent = Agent(
        name="parent",
        model=ScriptedProvider(
            [
                call("spawn_subagent", {"prompt": "  "}, call_id="c_spawn"),
                text("done"),
            ]
        ),
        plugins=[Subagents(_child([text("r")]))],
    )
    result = await Runner.run(parent, "go")
    assert "Nothing spawned" in _tool_result(result.entries, "c_spawn")
