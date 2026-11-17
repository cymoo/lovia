"""Suspended approvals: the run stops at the call, and a resume asks again.

``ApprovalRequired.suspend()`` is the third answer to an approval, next to
approve and reject: nobody can decide now. The runner lets calls already
running finish and persist (the ``BudgetExceeded`` path in ``_tool_phase``),
leaves the suspended call without a result, and raises ``RunSuspended`` with
the checkpoint ``interrupted`` — so a resume judges the call again.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lovia import (
    Agent,
    Approval,
    CancelToken,
    RunCancelled,
    Runner,
    RunSuspended,
    events,
    tool,
)
from lovia.approvals import ApprovalChannel
from lovia.checkpointer import CheckpointOptions
from lovia.runtime.loop import pending_tool_calls
from lovia.stores import InMemoryCheckpointer
from lovia.transcript import ToolResultEntry, TranscriptEntry

from ..scripted_provider import ScriptedProvider, batch, call, text


def _results(entries: list[TranscriptEntry]) -> dict[str, str]:
    return {e.call_id: str(e.output) for e in entries if isinstance(e, ToolResultEntry)}


def _pushing(runs: list[str]) -> Any:
    @tool(permission="ask")
    async def push() -> str:
        """Push to the remote."""
        runs.append("push")
        return "pushed"

    return push


async def test_streaming_suspend_stops_the_run_at_the_call() -> None:
    runs: list[str] = []
    cp = InMemoryCheckpointer()
    agent = Agent(
        name="t",
        model=ScriptedProvider([call("push", {}, call_id="c1"), text("done")]),
        tools=[_pushing(runs)],
    )
    handle = Runner.stream(agent, "go", checkpoint=CheckpointOptions(cp, "r1"))
    failed: list[BaseException] = []
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            ev.suspend("nobody is around")
        elif isinstance(ev, events.RunFailed):
            failed.append(ev.error)

    with pytest.raises(RunSuspended) as info:
        await handle.result()
    suspended = info.value
    assert suspended.call is not None and suspended.call.id == "c1"
    assert suspended.reason == "nobody is around"
    assert str(suspended) == "Tool push is waiting for approval: nobody is around"
    assert failed == [suspended]  # the stream ends as for any resumable stop
    assert runs == []

    snap = await cp.load("r1")
    assert snap is not None and snap.status == "interrupted"
    assert [c.id for c in pending_tool_calls(snap.entries)] == ["c1"]


async def test_a_resume_asks_again_and_the_answer_runs_the_call_once() -> None:
    runs: list[str] = []
    answers: dict[str, Approval] = {}
    asked: list[str] = []

    def handler(request: events.ApprovalRequired, ctx: Any) -> Any:
        asked.append(request.call.id)
        if (answer := answers.pop(request.call.id, None)) is not None:
            return answer
        request.suspend()

    cp = InMemoryCheckpointer()
    agent = Agent(
        name="t",
        model=ScriptedProvider([call("push", {}, call_id="c1"), text("done")]),
        tools=[_pushing(runs)],
        approval_handler=handler,
    )
    checkpoint = CheckpointOptions(cp, "r2")
    with pytest.raises(RunSuspended):
        await Runner.run(agent, "go", checkpoint=checkpoint)
    assert runs == []

    answers["c1"] = Approval(True)
    result = await Runner.run(agent, "go", checkpoint=checkpoint)
    assert result.output == "done"
    assert runs == ["push"]
    assert asked == ["c1", "c1"]  # the resume judged the call and asked again
    assert _results(result.entries)["c1"] == "pushed"


async def test_a_denied_answer_on_resume_reaches_the_model() -> None:
    runs: list[str] = []
    answers: dict[str, Approval] = {}

    def handler(request: events.ApprovalRequired, ctx: Any) -> Any:
        if (answer := answers.pop(request.call.id, None)) is not None:
            return answer
        request.suspend()

    cp = InMemoryCheckpointer()
    agent = Agent(
        name="t",
        model=ScriptedProvider([call("push", {}, call_id="c1"), text("ok")]),
        tools=[_pushing(runs)],
        approval_handler=handler,
    )
    checkpoint = CheckpointOptions(cp, "r3")
    with pytest.raises(RunSuspended):
        await Runner.run(agent, "go", checkpoint=checkpoint)

    answers["c1"] = Approval(False, "open a PR instead")
    result = await Runner.run(agent, "go", checkpoint=checkpoint)
    assert runs == []
    assert _results(result.entries)["c1"] == (
        "Tool push was not approved: open a PR instead"
    )


async def test_running_siblings_finish_first_and_are_not_rerun_on_resume() -> None:
    # Parallel by default: the allowed call is already running when the next
    # call's approval is suspended. Cancelling it would re-run it on resume.
    runs: list[str] = []

    @tool
    async def slow() -> str:
        await asyncio.sleep(0.05)
        runs.append("slow")
        return "slow done"

    answers: dict[str, Approval] = {}

    def handler(request: events.ApprovalRequired, ctx: Any) -> Any:
        if (answer := answers.pop(request.call.id, None)) is not None:
            return answer
        request.suspend()

    cp = InMemoryCheckpointer()
    agent = Agent(
        name="t",
        model=ScriptedProvider(
            [batch(("slow", {}, "c1"), ("push", {}, "c2")), text("done")]
        ),
        tools=[slow, _pushing(runs)],
        approval_handler=handler,
    )
    checkpoint = CheckpointOptions(cp, "r4")
    with pytest.raises(RunSuspended) as info:
        await Runner.run(agent, "go", checkpoint=checkpoint)
    assert info.value.call is not None and info.value.call.id == "c2"
    assert runs == ["slow"]
    snap = await cp.load("r4")
    assert snap is not None
    assert _results(snap.entries) == {"c1": "slow done"}
    assert [c.id for c in pending_tool_calls(snap.entries)] == ["c2"]

    answers["c2"] = Approval(True)
    result = await Runner.run(agent, "go", checkpoint=checkpoint)
    assert result.output == "done"
    assert runs == ["slow", "push"]  # the finished sibling did not run again


async def test_the_channel_suspends_by_call_id() -> None:
    cp = InMemoryCheckpointer()
    agent = Agent(
        name="t",
        model=ScriptedProvider([call("push", {}, call_id="c1"), text("done")]),
        tools=[_pushing([])],
    )
    handle = Runner.stream(agent, "go", checkpoint=CheckpointOptions(cp, "r5"))
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            handle.approvals.suspend(ev.call.id, reason="later")
    with pytest.raises(RunSuspended) as info:
        await handle.result()
    assert info.value.reason == "later"


async def test_a_resume_judges_the_call_afresh() -> None:
    # The permission loosened while the run waited: the resume runs the call
    # without asking. (A tightened one would refuse it the same way.)
    mode = {"push": "ask"}
    runs: list[str] = []
    asked: list[str] = []

    @tool(permission=lambda args, ctx: mode["push"])
    async def push() -> str:
        """Push to the remote."""
        runs.append("push")
        return "pushed"

    def handler(request: events.ApprovalRequired, ctx: Any) -> None:
        asked.append(request.call.id)
        request.suspend()

    cp = InMemoryCheckpointer()
    agent = Agent(
        name="t",
        model=ScriptedProvider([call("push", {}, call_id="c1"), text("done")]),
        tools=[push],
        approval_handler=handler,
    )
    checkpoint = CheckpointOptions(cp, "r6")
    with pytest.raises(RunSuspended):
        await Runner.run(agent, "go", checkpoint=checkpoint)

    mode["push"] = "allow"
    result = await Runner.run(agent, "go", checkpoint=checkpoint)
    assert result.output == "done"
    assert runs == ["push"]
    assert asked == ["c1"]


async def test_a_stop_beats_a_suspension() -> None:
    token = CancelToken()
    agent = Agent(
        name="t",
        model=ScriptedProvider([call("push", {}, call_id="c1"), text("done")]),
        tools=[_pushing([])],
    )
    handle = Runner.stream(agent, "go", cancel_token=token)
    async for ev in handle:
        if isinstance(ev, events.ApprovalRequired):
            token.cancel()
            ev.suspend()
    with pytest.raises(RunCancelled):
        await handle.result()


async def test_a_suspension_reads_as_a_denial_to_code_that_only_checks() -> None:
    # Fail closed: a consumer unaware of suspension sees approved=False.
    channel = ApprovalChannel()
    fut = channel.register("c1")
    channel.suspend("c1", reason="later")
    approval = fut.result()
    assert approval.approved is False and approval.reason == "later"
