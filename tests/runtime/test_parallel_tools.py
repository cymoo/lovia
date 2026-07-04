"""Parallel tool execution: overlap, barriers, and the preserved contracts.

One turn's tool calls run concurrently by default (``Tool.parallel=True``);
these tests pin down the concurrency itself plus every serial-era contract
the parallel ``_tool_phase`` must preserve: approval backpressure, handoff
first-wins, per-result checkpoint cadence, budget grace, cancellation, and
``call_id``-keyed pairing under completion-order results.
"""

from __future__ import annotations

import asyncio

import pytest

from lovia import (
    Agent,
    BudgetExceeded,
    CancelToken,
    RunBudget,
    RunCancelled,
    RunContext,
    Runner,
    tool,
)
from lovia import events
from lovia.checkpointer import CheckpointOptions, RunHead
from lovia.handoff import Handoff
from lovia.runtime.loop import pending_tool_calls
from lovia.stores import InMemoryCheckpointer
from lovia.transcript import ToolResultEntry, TranscriptEntry

from ..scripted_provider import ScriptedProvider, batch, text


def _tool_results(entries: list[TranscriptEntry]) -> dict[str, ToolResultEntry]:
    return {e.call_id: e for e in entries if isinstance(e, ToolResultEntry)}


async def test_parallel_tools_truly_overlap() -> None:
    # ``gated`` can only finish while ``opener`` runs at the same time. Under
    # serial execution gated times out into a tool error; under parallel
    # execution both succeed.
    gate = asyncio.Event()

    @tool
    async def gated() -> str:
        await asyncio.wait_for(gate.wait(), timeout=1)
        return "gated done"

    @tool
    async def opener() -> str:
        gate.set()
        return "opened"

    agent = Agent(
        name="p",
        model=ScriptedProvider([batch(("gated", {}), ("opener", {})), text("done")]),
        tools=[gated, opener],
    )
    result = await asyncio.wait_for(Runner.run(agent, "go"), timeout=2)
    assert result.output == "done"
    results = _tool_results(result.entries)
    assert not any(e.is_error for e in results.values())


async def test_sequential_tool_is_an_execution_barrier() -> None:
    # parallel=False waits out in-flight calls, runs alone, then later calls
    # proceed — even when the in-flight call is slower than the barrier.
    ticks: list[str] = []

    @tool
    async def slow_a() -> str:
        ticks.append("a:start")
        await asyncio.sleep(0.05)
        ticks.append("a:end")
        return "a"

    @tool(parallel=False)
    async def barrier_b() -> str:
        ticks.append("b:start")
        await asyncio.sleep(0.01)
        ticks.append("b:end")
        return "b"

    @tool
    async def par_c() -> str:
        ticks.append("c:start")
        ticks.append("c:end")
        return "c"

    agent = Agent(
        name="p",
        model=ScriptedProvider(
            [batch(("slow_a", {}), ("barrier_b", {}), ("par_c", {})), text("done")]
        ),
        tools=[slow_a, barrier_b, par_c],
    )
    result = await Runner.run(agent, "go")
    assert result.output == "done"
    assert ticks == ["a:start", "a:end", "b:start", "b:end", "c:start", "c:end"]


async def test_handoff_is_a_barrier_and_slow_sibling_still_lands() -> None:
    # A handoff requested alongside a slow tool waits for it (barrier), so the
    # slow tool's result is in the transcript before the agent switches.
    alpha = Agent(name="alpha", model=ScriptedProvider([text("from alpha")]))

    @tool
    async def slow() -> str:
        await asyncio.sleep(0.02)
        return "slow done"

    triage = Agent(
        name="triage",
        model=ScriptedProvider(
            [batch(("slow", {}, "c1"), ("transfer_to_alpha", {}, "h1"))]
        ),
        tools=[slow],
        handoffs=[alpha],
    )
    seen: list[str] = []
    handle = Runner.stream(triage, "route me")
    async for ev in handle:
        if isinstance(ev, events.ToolCallCompleted):
            seen.append(ev.call.id)
    result = await handle.result()
    assert result.final_agent.name == "alpha"
    assert result.output == "from alpha"
    results = _tool_results(result.entries)
    assert results["c1"].output == "slow done" and not results["c1"].is_error
    # Barrier ordering: the slow sibling completed before the handoff ran.
    assert seen == ["c1", "h1"]


async def test_first_handoff_wins_and_plain_tool_still_runs() -> None:
    # The parallel extension of the serial first-wins contract: the loser is
    # rejected unrun (its on_handoff never fires) and a non-handoff call after
    # both still executes.
    fired: list[str] = []
    alpha = Agent(name="alpha", model=ScriptedProvider([text("from alpha")]))
    beta = Agent(name="beta", model=ScriptedProvider([text("from beta")]))

    @tool
    async def plain() -> str:
        return "plain ran"

    triage = Agent(
        name="triage",
        model=ScriptedProvider(
            [
                batch(
                    ("transfer_to_alpha", {}, "h1"),
                    ("transfer_to_beta", {}, "h2"),
                    ("plain", {}, "c3"),
                )
            ]
        ),
        tools=[plain],
        handoffs=[
            Handoff(target=alpha, on_handoff=lambda a, c: fired.append("alpha")),
            Handoff(target=beta, on_handoff=lambda a, c: fired.append("beta")),
        ],
    )
    result = await Runner.run(triage, "route me")
    assert result.final_agent.name == "alpha"
    assert fired == ["alpha"]
    results = _tool_results(result.entries)
    assert "Transferred to alpha" in results["h1"].output
    assert results["h2"].is_error
    assert "already transferred to 'alpha'" in results["h2"].output
    assert results["c3"].output == "plain ran"


async def test_approval_mid_batch_keeps_backpressure() -> None:
    # The approval gate must not wait for in-flight siblings: ApprovalRequired
    # reaches the consumer while the slow tool is still running, and resolving
    # it on the event (before advancing the stream) still works.
    @tool
    async def slow_par() -> str:
        await asyncio.sleep(0.05)
        return "slow done"

    @tool(needs_approval=True)
    async def sensitive() -> str:
        return "secret done"

    agent = Agent(
        name="p",
        model=ScriptedProvider(
            [batch(("slow_par", {}, "c1"), ("sensitive", {}, "c2")), text("done")]
        ),
        tools=[slow_par, sensitive],
    )
    order: list[str] = []
    handle = Runner.stream(agent, "go")
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            order.append("approval_required")
            ev.approve()
        elif isinstance(ev, events.ToolCallCompleted):
            order.append(f"completed:{ev.call.id}")
    result = await handle.result()
    assert result.output == "done"
    results = _tool_results(result.entries)
    assert results["c2"].output == "secret done" and not results["c2"].is_error
    # The gate fired before the in-flight sibling finished.
    assert order.index("approval_required") < order.index("completed:c1")


async def test_run_cancelled_in_one_tool_cancels_siblings() -> None:
    # RunCancelled escaping a tool (e.g. an agent-as-tool sub-run tripping the
    # inherited token) aborts the batch promptly: in-flight siblings receive
    # CancelledError instead of running to completion.
    cancelled: list[str] = []

    @tool
    async def sleeper() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append("sleeper")
            raise
        return "never"

    @tool
    async def raiser() -> str:
        await asyncio.sleep(0.01)
        raise RunCancelled("tool says stop")

    agent = Agent(
        name="p",
        model=ScriptedProvider(
            [batch(("sleeper", {}, "c1"), ("raiser", {}, "c2")), text("never")]
        ),
        tools=[sleeper, raiser],
    )
    with pytest.raises(RunCancelled):
        await asyncio.wait_for(Runner.run(agent, "go"), timeout=2)
    assert cancelled == ["sleeper"]


async def test_token_cancel_lands_at_the_next_completed_result() -> None:
    # A cancel() issued mid-batch (here: by the first tool) stops the batch at
    # the drain point instead of letting the slow sibling run for 30s.
    token = CancelToken()

    @tool
    async def canceller(ctx: RunContext) -> str:
        ctx.cancel_token.cancel("stop after me")
        return "did it"

    @tool
    async def slowpoke() -> str:
        await asyncio.sleep(30)
        return "never"

    agent = Agent(
        name="p",
        model=ScriptedProvider(
            [batch(("canceller", {}, "c1"), ("slowpoke", {}, "c2")), text("never")]
        ),
        tools=[canceller, slowpoke],
    )
    with pytest.raises(RunCancelled):
        await asyncio.wait_for(Runner.run(agent, "go", cancel_token=token), timeout=2)


class _RecordingCheckpointer(InMemoryCheckpointer):
    """Counts appends and remembers each delta for cadence assertions."""

    def __init__(self, fail_on_result_append: int | None = None) -> None:
        super().__init__()
        self.deltas: list[list[TranscriptEntry]] = []
        self._fail_on = fail_on_result_append
        self._result_appends = 0

    async def append(
        self, run_id: str, entries: list[TranscriptEntry], head: RunHead
    ) -> None:
        if any(isinstance(e, ToolResultEntry) for e in entries):
            self._result_appends += 1
            if self._fail_on is not None and self._result_appends >= self._fail_on:
                raise ConnectionError("store down")
        self.deltas.append(list(entries))
        await super().append(run_id, entries, head)


async def test_checkpoint_saves_each_result_incrementally() -> None:
    @tool
    async def t1() -> str:
        return "one"

    @tool
    async def t2() -> str:
        return "two"

    @tool
    async def t3() -> str:
        return "three"

    cp = _RecordingCheckpointer()
    agent = Agent(
        name="p",
        model=ScriptedProvider(
            [batch(("t1", {}), ("t2", {}), ("t3", {})), text("done")]
        ),
        tools=[t1, t2, t3],
    )
    result = await Runner.run(agent, "go", checkpoint=CheckpointOptions(cp, "run-1"))
    assert result.output == "done"
    # All three results reached the store incrementally: every delta is a
    # monotonic append and their union carries exactly the three results.
    stored = [e for delta in cp.deltas for e in delta if isinstance(e, ToolResultEntry)]
    assert sorted(e.call_id for e in stored) == [
        "call_0_t1",
        "call_1_t2",
        "call_2_t3",
    ]


async def test_checkpoint_store_failure_aborts_the_batch() -> None:
    # save_running failures must abort (durability contract), also mid-batch.
    @tool
    async def fast() -> str:
        return "fast"

    @tool
    async def slow() -> str:
        await asyncio.sleep(5)
        return "slow"

    cp = _RecordingCheckpointer(fail_on_result_append=1)
    agent = Agent(
        name="p",
        model=ScriptedProvider(
            [batch(("fast", {}, "c1"), ("slow", {}, "c2")), text("never")]
        ),
        tools=[fast, slow],
    )
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(
            Runner.run(agent, "go", checkpoint=CheckpointOptions(cp, "run-2")),
            timeout=2,
        )


async def test_completion_order_results_still_pair_by_call_id() -> None:
    # The first-requested call finishes last; completions, transcript order,
    # and the next model call all stay consistent because pairing is by id.
    gate = asyncio.Event()

    @tool
    async def first_listed() -> str:
        await asyncio.wait_for(gate.wait(), timeout=1)
        return "first result"

    @tool
    async def second_listed() -> str:
        gate.set()
        return "second result"

    provider = ScriptedProvider(
        [batch(("first_listed", {}, "c1"), ("second_listed", {}, "c2")), text("done")]
    )
    agent = Agent(name="p", model=provider, tools=[first_listed, second_listed])
    completed: list[str] = []
    handle = Runner.stream(agent, "go")
    async for ev in handle:
        if isinstance(ev, events.ToolCallCompleted):
            completed.append(ev.call.id)
    result = await handle.result()
    assert result.output == "done"
    assert completed == ["c2", "c1"]  # completion order, not request order
    result_entries = [e for e in result.entries if isinstance(e, ToolResultEntry)]
    assert [e.call_id for e in result_entries] == ["c2", "c1"]
    # The next model call saw both results, correctly paired by call_id.
    tool_messages = {
        m.tool_call_id: m.content for m in provider.calls[-1] if m.role == "tool"
    }
    assert tool_messages == {"c1": "first result", "c2": "second result"}


async def test_error_occurred_carries_the_failing_call() -> None:
    @tool
    async def boom() -> str:
        raise ValueError("kaput")

    @tool
    async def fine() -> str:
        return "ok"

    agent = Agent(
        name="p",
        model=ScriptedProvider(
            [batch(("boom", {}, "c1"), ("fine", {}, "c2")), text("done")]
        ),
        tools=[boom, fine],
    )
    errors: list[events.ToolCallFailed] = []
    handle = Runner.stream(agent, "go")
    async for ev in handle:
        if isinstance(ev, events.ToolCallFailed):
            errors.append(ev)
    result = await handle.result()
    assert result.output == "done"
    assert len(errors) == 1
    assert errors[0].call is not None and errors[0].call.id == "c1"


async def test_all_sequential_batch_reproduces_the_serial_stream() -> None:
    # The degenerate case — every tool parallel=False — must be byte-for-byte
    # the serial loop: strictly nested Started/Completed pairs in request order.
    @tool(parallel=False)
    async def s1() -> str:
        return "one"

    @tool(parallel=False)
    async def s2() -> str:
        return "two"

    agent = Agent(
        name="p",
        model=ScriptedProvider(
            [batch(("s1", {}, "c1"), ("s2", {}, "c2")), text("done")]
        ),
        tools=[s1, s2],
    )
    tool_events: list[str] = []
    handle = Runner.stream(agent, "go")
    async for ev in handle:
        if isinstance(ev, events.ToolCallStarted):
            tool_events.append(f"started:{ev.call.id}")
        elif isinstance(ev, events.ToolCallCompleted):
            tool_events.append(f"completed:{ev.call.id}")
    result = await handle.result()
    assert result.output == "done"
    assert tool_events == ["started:c1", "completed:c1", "started:c2", "completed:c2"]


async def test_budget_abort_lets_inflight_finish_and_resume_drains_the_rest() -> None:
    # Tripping max_tool_calls stops spawning but lets the in-flight call
    # finish and persist; a resume re-executes only the never-started call.
    runs: list[str] = []

    @tool
    async def slow() -> str:
        await asyncio.sleep(0.05)
        runs.append("slow")
        return "slow done"

    @tool
    async def other() -> str:
        runs.append("other")
        return "other done"

    cp = InMemoryCheckpointer()
    agent = Agent(
        name="p",
        model=ScriptedProvider(
            [batch(("slow", {}, "c1"), ("other", {}, "c2")), text("done")]
        ),
        tools=[slow, other],
    )
    with pytest.raises(BudgetExceeded):
        await Runner.run(
            agent,
            "go",
            budget=RunBudget(max_tool_calls=1),
            checkpoint=CheckpointOptions(cp, "run-3"),
        )
    assert runs == ["slow"]  # in-flight finished; the second call never started
    snap = await cp.load("run-3")
    assert snap is not None
    results = _tool_results(snap.entries)
    assert results["c1"].output == "slow done"
    assert "c2" not in results  # dangling, waiting for a resume

    resumed = await Runner.run(
        agent,
        "go",
        budget=RunBudget(max_tool_calls=10),
        checkpoint=CheckpointOptions(cp, "run-3"),
    )
    assert resumed.output == "done"
    assert runs == ["slow", "other"]  # the finished call was NOT re-executed


async def test_duplicate_call_ids_in_one_batch_pair_by_occurrence() -> None:
    @tool
    async def t_a() -> str:
        return "a done"

    @tool
    async def t_b() -> str:
        return "b done"

    agent = Agent(
        name="p",
        model=ScriptedProvider(
            [batch(("t_a", {}, "dup"), ("t_b", {}, "dup")), text("done")]
        ),
        tools=[t_a, t_b],
    )
    result = await Runner.run(agent, "go")
    assert result.output == "done"
    result_entries = [e for e in result.entries if isinstance(e, ToolResultEntry)]
    assert len(result_entries) == 2
    assert all(e.call_id == "dup" for e in result_entries)
    assert pending_tool_calls(result.entries) == []
