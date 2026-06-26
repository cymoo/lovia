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

from lovia import Agent, tool  # noqa: E402
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
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)


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
