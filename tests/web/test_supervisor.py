"""Integration tests for the run supervisor: detach, re-attach, cancel, cap.

httpx's ASGITransport buffers the whole SSE body, so a blocked run is consumed
in a concurrent task while the test drives release/approve/cancel from the main
flow (the same shape as the approval test in ``test_web``).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402

from lovia import Agent, Mailbox, Runner, tool  # noqa: E402
from lovia.reliability import RunBudget  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_web import _app, _parse_sse  # noqa: E402


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _blocking_tool(release: asyncio.Event):
    @tool
    async def block() -> str:
        """Block until the test releases it."""
        await release.wait()
        return "unblocked"

    return block


def _spawn(ac, path, *, params=None, json=None):
    """Consume an SSE endpoint in a task; returns (task, lines)."""
    lines: list[str] = []

    async def run() -> None:
        async with ac.stream("POST", path, params=params, json=json) as res:
            async for line in res.aiter_lines():
                lines.append(line)

    return asyncio.create_task(run()), lines


async def _wait_run(ac, sid, *, status=None, gone=False):
    for _ in range(250):
        runs = (await ac.get("/api/runs")).json()
        match = next((r for r in runs if r["session_id"] == sid), None)
        if gone and match is None:
            return None
        if (
            not gone
            and match is not None
            and (status is None or match["status"] == status)
        ):
            return match
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"run condition not met (sid={sid}, status={status}, gone={gone})"
    )


async def _kill(task) -> None:
    task.cancel()
    # Only swallow the expected cancellation — a timeout (hung task) or an
    # unexpected error should surface and fail the test, not be hidden.
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)


@pytest.mark.asyncio
async def test_run_survives_disconnect_and_completes_headless() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")  # the supervised run came alive
        await _kill(task)  # DISCONNECT
        await _wait_run(ac, "s1")  # survived the disconnect (not cancelled)
        release.set()
        await _wait_run(ac, "s1", gone=True)  # ran to completion with no client
        detail = (await ac.get("/api/sessions/s1")).json()
    assert len(provider.calls) == 2
    assert any(
        m["role"] == "assistant" and "done" in str(m.get("content"))
        for m in detail["entries"]
    )


@pytest.mark.asyncio
async def test_reattach_and_co_watch() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent)
    async with _client(app) as ac:
        t0, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        # Two more clients re-attach to the same live run.
        t1, l1 = _spawn(ac, "/api/chat/reconnect", params={"session_id": "s1"})
        t2, l2 = _spawn(ac, "/api/chat/reconnect", params={"session_id": "s1"})
        await asyncio.sleep(0.1)  # let both attach
        release.set()
        await asyncio.wait_for(asyncio.gather(t0, t1, t2), timeout=5)
    for lines in (l1, l2):
        kinds = [e for e, _ in _parse_sse("\n".join(lines))]
        assert "snapshot" in kinds  # authoritative re-attach snapshot
        assert kinds[-1] == "done"


@pytest.mark.asyncio
async def test_detached_approval_blocks_then_resolves_on_reattach() -> None:
    @tool(needs_approval=True)
    async def sensitive() -> str:
        """Sensitive."""
        return "did it"

    provider = ScriptedProvider([call("sensitive", {}, call_id="c1"), text("ack")])
    agent = Agent(name="bot", model=provider, tools=[sensitive])
    app = _app(agent)
    async with _client(app) as ac:
        t0, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", status="blocked_on_approval")  # parked, not denied
        await _kill(t0)  # disconnect while awaiting approval
        await _wait_run(ac, "s1", status="blocked_on_approval")  # still pending

        t1, l1 = _spawn(ac, "/api/chat/reconnect", params={"session_id": "s1"})
        await asyncio.sleep(0.1)  # let it attach (re-emits the pending approval)
        await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "approve"},
        )
        await asyncio.wait_for(t1, timeout=5)
    evs = _parse_sse("\n".join(l1))
    kinds = [e for e, _ in evs]
    assert "approval_required" in kinds  # re-emitted on re-attach
    assert kinds[-1] == "done"
    assert any(d.get("result") == "did it" for (e, d) in evs if e == "tool_result")


@pytest.mark.asyncio
async def test_cancel_a_detached_run() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        await ac.post("/api/chat/cancel", params={"session_id": "s1"})
        release.set()
        await _wait_run(ac, "s1", gone=True)  # cancelled run is gone
        await _kill(task)
    assert len(provider.calls) == 1  # never reached turn 2


@pytest.mark.asyncio
async def test_concurrency_cap_rejects_new_runs() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent, max_background_runs=1)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        r = await ac.post(
            "/api/chat/stream", json={"message": "go", "session_id": "s2"}
        )
        assert r.status_code == 429
        release.set()
        await _kill(task)


@pytest.mark.asyncio
async def test_default_budget_trips_an_abandoned_run() -> None:
    @tool
    async def noop() -> str:
        """noop."""
        return "ok"

    provider = ScriptedProvider([call("noop", {}, call_id=f"c{i}") for i in range(6)])
    agent = Agent(name="bot", model=provider, tools=[noop])
    app = _app(agent, default_budget_factory=lambda: RunBudget(max_total_tokens=1))
    async with _client(app) as ac:
        lines: list[str] = []
        async with ac.stream(
            "POST", "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        ) as res:
            async for line in res.aiter_lines():
                lines.append(line)
    kinds = [e for e, _ in _parse_sse("\n".join(lines))]
    assert "error" in kinds  # budget exceeded surfaced as a clean error
    assert len(provider.calls) <= 2


@pytest.mark.asyncio
async def test_restart_reconnect_resumes_from_checkpoint() -> None:
    from lovia.checkpointer import RunHead
    from lovia.messages import Usage
    from lovia.transcript import AssistantTextEntry, InputEntry

    store = ChatStore.in_memory()
    sid = "s1"
    # Prior history + an interrupted run in the checkpoint (no live controller —
    # as if the process restarted with an empty supervisor).
    await store.session.append(
        sid,
        [InputEntry(role="user", content="q1"), AssistantTextEntry(content="a1")],
    )
    await store.upsert(sid, agent="bot")
    await store.checkpointer.append(
        "run-1",
        [InputEntry(role="user", content="q2")],
        RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
    )
    await store.set_active_run_id(sid, "run-1")

    provider = ScriptedProvider([text("resumed-answer")])
    app = _app(Agent(name="bot", model=provider), store=store)
    async with _client(app) as ac:
        lines: list[str] = []
        async with ac.stream(
            "POST", "/api/chat/reconnect", params={"session_id": sid}
        ) as res:
            async for line in res.aiter_lines():
                lines.append(line)
    evs = _parse_sse("\n".join(lines))
    kinds = [e for e, _ in evs]
    assert kinds[-1] == "done"
    assert (
        "".join(d["delta"] for (e, d) in evs if e == "text_delta") == "resumed-answer"
    )


@pytest.mark.asyncio
async def test_shutdown_leaves_a_resumable_checkpoint() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    store = ChatStore.in_memory()
    app = _app(agent, store=store)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        # Graceful shutdown (deploy/restart): cooperative stop, short grace.
        await app.state.deps.supervisor.shutdown(grace=0.2)
        await _kill(task)
        # Drained from the supervisor, but left resumable (pointer + a
        # running/interrupted checkpoint — only a user cancel deletes it).
        assert app.state.deps.supervisor.get("s1") is None
    rid = await store.get_active_run_id("s1")
    assert rid is not None
    snap = await store.checkpointer.load(rid)
    assert snap is not None and snap.status in ("interrupted", "running")


@pytest.mark.asyncio
async def test_cancel_during_auto_chain_hop_does_not_revive(monkeypatch) -> None:
    """A user cancel landing in the auto-chain transition must not revive.

    On an auto-chain hop the next leg's checkpoint pointer is advanced
    (set_active_run_id) one step before ``self.run_id`` catches up, so the cancel
    endpoint's eager clear reads a stale ``ctrl.run_id`` and its guarded clear
    no-ops against the already-advanced pointer. The supervised loop must then
    bail at the top of the next leg, so that leg never starts and never persists
    an ``interrupted`` checkpoint a reconnect could revive. We pin the fix by
    asserting the chained leg never runs (``Runner.stream`` is called once) and
    that nothing is left to reconnect to.
    """
    from lovia.messages import Usage
    from lovia.transcript import FinishDelta, TextDelta, UsageDelta
    from lovia.web import supervisor as sup_mod

    # One mailbox for the controller so the provider can leave an auto-chain
    # leftover: a push *during* the model call lands after the turn-start drain,
    # so it stays queued for the next leg (same shape as test_steering's late
    # push). That queued message is what makes leg 1 auto-chain to leg 2.
    mailbox = Mailbox()
    monkeypatch.setattr(sup_mod, "Mailbox", lambda: mailbox)

    class LatePushProvider:
        name = "bot"
        supports_json_schema = False

        async def stream(
            self, entries, *, tools=None, response_format=None, settings=None
        ):
            mailbox.push("q2")  # after the turn-start drain → queued as leftover
            yield TextDelta(text="done")
            yield UsageDelta(usage=Usage(input_tokens=1, output_tokens=1))
            yield FinishDelta(reason="stop")

    # Park the task at the auto-chain pointer advance (the 2nd set_active_run_id,
    # i.e. the leg 1 → leg 2 hop) with the DB pointer already moved, so a cancel
    # fired now sees the stale-ctrl.run_id / advanced-pointer window.
    store = ChatStore.in_memory()
    parked = asyncio.Event()
    release_hop = asyncio.Event()
    orig_set = store.set_active_run_id
    sets = 0

    async def gated_set(sid, run_id):
        nonlocal sets
        await orig_set(sid, run_id)  # advance the DB pointer first (DB = leg 2)
        sets += 1
        if sets == 2:
            parked.set()
            await release_hop.wait()

    monkeypatch.setattr(store, "set_active_run_id", gated_set)

    # Count legs that actually start. The runner checks cancel at the top of the
    # turn *before* the model call, so the provider isn't a reliable leg counter;
    # Runner.stream (one call per leg) is.
    streams = 0
    orig_stream = Runner.stream

    def counting_stream(*a, **k):
        nonlocal streams
        streams += 1
        return orig_stream(*a, **k)

    monkeypatch.setattr(Runner, "stream", counting_stream)

    agent = Agent(name="bot", model=LatePushProvider())
    app = _app(agent, store=store)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await asyncio.wait_for(parked.wait(), timeout=5)  # leg 1 done, at the hop
        await ac.post("/api/chat/cancel", params={"session_id": "s1"})
        release_hop.set()
        await asyncio.wait_for(task, timeout=5)  # winds down, hub closes the SSE

        assert streams == 1  # the chained leg never started
        assert await store.get_active_run_id("s1") is None  # pointer cleared
        r = await ac.post("/api/chat/reconnect", params={"session_id": "s1"})
        assert r.status_code == 404  # nothing to revive
