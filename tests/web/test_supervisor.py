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
from lovia.exceptions import ProviderError  # noqa: E402
from lovia.reliability import RetryPolicy, RunBudget  # noqa: E402
from lovia.transcript import InputEntry, ToolResultEntry  # noqa: E402
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


async def _wait_session_entries(store, sid, *, timeout=5.0):
    """Poll until the session holds persisted entries.

    The supervisor's terminal persist runs in the run task's ``finally`` — which
    completes *after* the cancel endpoint returns and the controller is evicted —
    so a test can't sync on ``_wait_run(gone=True)`` for it.
    """
    for _ in range(int(timeout / 0.02)):
        entries = await store.session.load(sid)
        if entries:
            return entries
        await asyncio.sleep(0.02)
    raise AssertionError(f"session {sid} never persisted any entries")


async def _wait_calls(provider, n, *, timeout=5.0):
    """Poll until the scripted provider has been called at least ``n`` times."""
    for _ in range(int(timeout / 0.02)):
        if len(provider.calls) >= n:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"provider reached {len(provider.calls)} calls, wanted {n}")


class _ScriptThenFail(ScriptedProvider):
    """Replay the script, then raise a non-retryable :class:`ProviderError` on the
    next model call — a 'failed', non-resumable run end (not a clean cancel)."""

    async def stream(self, entries, *, tools=None, response_format=None, settings=None):
        if not self._script:
            err = ProviderError("scripted non-retryable failure")
            err.retryable = False
            raise err
        async for delta in super().stream(
            entries, tools=tools, response_format=response_format, settings=settings
        ):
            yield delta


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
async def test_configured_supervised_budget_trips_a_run() -> None:
    @tool
    async def noop() -> str:
        """noop."""
        return "ok"

    provider = ScriptedProvider([call("noop", {}, call_id=f"c{i}") for i in range(6)])
    agent = Agent(name="bot", model=provider, tools=[noop])
    app = _app(agent, budget=RunBudget(max_total_tokens=1))
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


def test_configured_budget_is_copied_per_run() -> None:
    # A configured ``budget`` is a template: ``fresh_budget`` hands each run its
    # own copy, so a RunBudget's wall-clock/tool-call state never bleeds across
    # runs (the failure mode when one instance was passed to every run).
    agent = Agent(name="bot", model=ScriptedProvider([text("hi")]))
    deps = _app(agent, budget=RunBudget(max_tool_calls=3)).state.deps
    a, b = deps.fresh_budget(), deps.fresh_budget()
    assert a is not b and a is not deps.budget  # a fresh copy per run...
    assert a.max_tool_calls == 3  # ...with the configured limits preserved
    assert _app(agent).state.deps.fresh_budget() is None  # unset -> unbounded


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
    # The record is keyed by the snapshot's run_id (via resolved_run_id), so the
    # resumed run finalizes ONE row under the original id — no duplicate.
    rec = await _wait_record(store, "run-1")
    assert rec.status == "completed"
    assert [r.id for r in await store.list_runs(session_id=sid)] == ["run-1"]


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
    # A resumable interrupt stays ONLY in the checkpoint — never also written to
    # the Session, or a resume (history + snapshot) would double-count the run.
    assert await store.session.load("s1") == []


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


@pytest.mark.asyncio
async def test_cancel_persists_partial_transcript() -> None:
    """A user stop folds the run's completed turns into the Session, so a page
    reload shows the partial chat instead of just its title over an empty body.

    The first turn (``ping``) completes before the second (``block``) parks the
    run, so the mirror holds a whole, finished turn to persist.
    """
    release = asyncio.Event()

    @tool
    async def ping() -> str:
        """Return immediately."""
        return "pong"

    provider = ScriptedProvider(
        [call("ping", {}, call_id="c1"), call("block", {}, call_id="c2"), text("end")]
    )
    agent = Agent(name="bot", model=provider, tools=[ping, _blocking_tool(release)])
    store = ChatStore.in_memory()
    app = _app(agent, store=store)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_calls(provider, 2)  # ping turn done; block turn now parked
        await ac.post("/api/chat/cancel", params={"session_id": "s1"})
        release.set()  # let the parked tool unwind so the run task can finish
        await _wait_run(ac, "s1", gone=True)
        await _kill(task)
        # The stopped run's completed turn survives in the durable Session...
        entries = await _wait_session_entries(store, "s1")
        assert any(isinstance(e, InputEntry) and e.content == "go" for e in entries)
        assert any(
            isinstance(e, ToolResultEntry) and e.output == "pong" for e in entries
        )
        # ...and nothing is left to reconnect to (one durable copy, no resume).
        assert await store.get_active_run_id("s1") is None
        detail = (await ac.get("/api/sessions/s1")).json()
    assert detail["active_run_id"] is None
    assert detail["entries"]  # the chat is no longer empty on reload


@pytest.mark.asyncio
async def test_failed_run_persists_partial_transcript() -> None:
    """A non-resumable failure (non-retryable provider error) folds the run's
    completed turns into the Session and leaves nothing to reconnect to — the
    'failed' checkpoint would otherwise be silently dropped by the next GET."""

    @tool
    async def ping() -> str:
        """Return immediately."""
        return "pong"

    # Turn 1 (ping) completes; the turn-2 model call raises a non-retryable error.
    provider = _ScriptThenFail([call("ping", {}, call_id="c1")])
    agent = Agent(name="bot", model=provider, tools=[ping])
    store = ChatStore.in_memory()
    app = _app(agent, store=store, retry=RetryPolicy(max_attempts=1))
    async with _client(app) as ac:
        lines: list[str] = []
        async with ac.stream(
            "POST", "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        ) as res:
            async for line in res.aiter_lines():
                lines.append(line)
        assert "error" in [e for e, _ in _parse_sse("\n".join(lines))]
        entries = await _wait_session_entries(store, "s1")
        assert any(
            isinstance(e, ToolResultEntry) and e.output == "pong" for e in entries
        )
        # The non-resumable run is folded into the Session, not stranded in a
        # checkpoint: no pointer, and reconnect finds nothing.
        assert await store.get_active_run_id("s1") is None
        r = await ac.post("/api/chat/reconnect", params={"session_id": "s1"})
        assert r.status_code == 404
        detail = (await ac.get("/api/sessions/s1")).json()
    assert detail["active_run_id"] is None
    assert detail["entries"]


@pytest.mark.asyncio
async def test_persist_partial_trims_dangling_resumed_tool_call() -> None:
    """A *resumed* run seeds its mirror with the checkpoint's entries, which can
    end on a tool call the restored run had not yet executed. If it's stopped
    before draining that call, the persisted transcript must not end on an
    unmatched ``tool_use`` (a provider would reject it on the next turn)."""
    from lovia.transcript import (
        AssistantTextEntry,
        ToolCallEntry,
        entries_to_messages,
    )
    from lovia.web.supervisor import RunController

    store = ChatStore.in_memory()
    agent = Agent(name="bot", model=ScriptedProvider([]))
    deps = _app(agent, store=store).state.deps
    ctrl = RunController(
        deps=deps,
        supervisor=deps.supervisor,
        session_id="s1",
        agent=agent,
        first_input="",
        first_checkpoint=None,
        seed_entries=[],
        is_new=False,
        title_message=None,
        source="user",
    )
    # The mirror a resumed-then-stopped run would carry: a pending, unexecuted call.
    ctrl.completed_mirror = [
        InputEntry(role="user", content="go"),
        AssistantTextEntry(content="on it"),
        ToolCallEntry(call_id="c1", name="block", arguments="{}"),
    ]
    await ctrl._persist_partial("run-x")

    entries = await store.session.load("s1")
    assert entries  # the partial chat survived the stop
    msgs = entries_to_messages(entries)
    assert not any(m.tool_calls for m in msgs)  # the dangling call was trimmed
    assert any(m.role == "user" and m.content == "go" for m in msgs)
    assert any(m.role == "assistant" and m.content == "on it" for m in msgs)


# ------------------------------------------------------------------ phase-1 -


@pytest.mark.asyncio
async def test_start_conflicts_when_session_already_reserved() -> None:
    from fastapi import HTTPException

    agent = Agent(name="bot", model=ScriptedProvider([text("hi")]))
    app = _app(agent)
    deps = app.state.deps
    await deps.supervisor.start(
        session_id="s1", agent=agent, input="go", is_new=True, title_message=None
    )
    # A second start for the same session must 409, not silently orphan the
    # first controller (two tabs submitting at once race exactly like this).
    with pytest.raises(HTTPException) as ei:
        await deps.supervisor.start(
            session_id="s1",
            agent=agent,
            input="again",
            is_new=False,
            title_message=None,
        )
    assert ei.value.status_code == 409
    assert deps.supervisor.cancel("s1")


@pytest.mark.asyncio
async def test_delete_session_stops_live_run_without_orphan_transcript() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        res = await ac.delete("/api/sessions/s1")
        assert res.status_code == 200
        await _wait_run(ac, "s1", gone=True)
        release.set()
        # The SSE closes only after the run task's finally (persist decision
        # included) has executed — safe to assert "nothing re-appeared" after.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    store = app.state.store
    assert await store.get("s1") is None  # metadata stays gone
    assert await store.session.load("s1") == []  # no orphan transcript rows
    assert len(provider.calls) == 1  # the run never reached turn 2


@pytest.mark.asyncio
async def test_delete_all_sessions_stops_live_runs() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        assert (await ac.delete("/api/sessions")).status_code == 200
        await _wait_run(ac, "s1", gone=True)
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert (await ac.get("/api/sessions")).json() == []
    assert await app.state.store.session.load("s1") == []


@pytest.mark.asyncio
async def test_approval_timeout_denies_and_completes_run() -> None:
    @tool(needs_approval=True)
    async def sensitive() -> str:
        """Sensitive."""
        return "did it"

    provider = ScriptedProvider([call("sensitive", {}, call_id="c1"), text("ack")])
    agent = Agent(name="bot", model=provider, tools=[sensitive])
    app = _app(agent, approval_timeout=0.3)
    async with _client(app) as ac:
        task, lines = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", status="blocked_on_approval")
        # Nobody approves: the timeout must deny and let the run finish.
        await asyncio.wait_for(task, timeout=5)
        await _wait_run(ac, "s1", gone=True)
    evs = _parse_sse("\n".join(lines))
    kinds = [e for e, _ in evs]
    assert "approval_required" in kinds
    assert kinds[-1] == "done"
    denied = [d for (e, d) in evs if e == "tool_result"]
    assert denied and "not approved" in str(denied[0].get("result"))
    assert not any("did it" in str(d.get("result")) for d in denied)


@pytest.mark.asyncio
async def test_blocking_chat_conflicts_with_live_run() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent)
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        # The non-streaming endpoint must not race a second run onto the session.
        res = await ac.post("/api/chat", json={"message": "hi", "session_id": "s1"})
        assert res.status_code == 409
        release.set()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_failed_start_releases_window_attachers(monkeypatch) -> None:
    """If checkpoint init fails mid-start, a subscriber that attached during the
    reservation window must be released (hub closed), not left hanging on a
    task that will never begin."""
    agent = Agent(name="bot", model=ScriptedProvider([text("hi")]))
    app = _app(agent)
    deps = app.state.deps

    entered = asyncio.Event()
    proceed = asyncio.Event()

    async def failing_set_active(self, session_id, run_id):
        entered.set()
        await proceed.wait()
        raise RuntimeError("db down")

    monkeypatch.setattr(type(deps.store), "set_active_run_id", failing_set_active)

    start_task = asyncio.create_task(
        deps.supervisor.start(
            session_id="s1", agent=agent, input="go", is_new=True, title_message=None
        )
    )
    await entered.wait()
    live = deps.supervisor.get("s1")
    assert live is not None  # reserved during the window
    attachment = live.attach(with_snapshot=True)

    proceed.set()
    with pytest.raises(RuntimeError, match="db down"):
        await start_task
    assert deps.supervisor.get("s1") is None  # reservation rolled back
    # The window attacher's subscription ends promptly instead of hanging.
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(attachment.subscription.__anext__(), timeout=2)


# ------------------------------------------------------ durable run records -


async def _wait_record(store, run_id, *, timeout=5.0):
    """Poll until the run record reaches a terminal status (the finalizing
    write lands in the run task's wind-down, after eviction)."""
    for _ in range(int(timeout / 0.02)):
        recs = {r.id: r for r in await store.list_runs()}
        rec = recs.get(run_id)
        if rec is not None and rec.status != "running":
            return rec
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} never recorded a terminal status")


@pytest.mark.asyncio
async def test_completed_run_leaves_a_completed_record() -> None:
    provider = ScriptedProvider([text("done")])
    agent = Agent(name="bot", model=provider)
    app = _app(agent)
    store = app.state.store
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", gone=True)
        await task

        recs = (await ac.get("/api/runs/history")).json()
        scoped = (
            await ac.get("/api/runs/history", params={"session_id": "s1"})
        ).json()
        other = (
            await ac.get("/api/runs/history", params={"session_id": "nope"})
        ).json()
    assert len(recs) == 1
    rec = recs[0]
    assert rec["session_id"] == "s1" and rec["agent"] == "bot"
    assert rec["source"] == "user"  # interactive start
    assert rec["status"] == "completed" and rec["error"] is None
    assert rec["finished_at"] >= rec["started_at"]
    # The session_id filter is what the context-ring restore queries on reload.
    assert scoped == recs and other == []
    stored = await _wait_record(store, rec["run_id"])
    assert stored.status == "completed"
    # Usage lands in the record — token totals plus the final prompt size the
    # restored ring measures fill with (scripted turns report input_tokens=1).
    assert stored.usage["input_tokens"] == 1
    assert stored.usage["last_input_tokens"] == 1


@pytest.mark.asyncio
async def test_cancelled_run_leaves_a_cancelled_record() -> None:
    release = asyncio.Event()
    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[_blocking_tool(release)])
    app = _app(agent)
    store = app.state.store
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        run_id = (await ac.get("/api/runs")).json()[0]["run_id"]
        await ac.post("/api/chat/cancel", params={"session_id": "s1"})
        release.set()
        await _kill(task)

        rec = await _wait_record(store, run_id)
    assert rec.status == "cancelled" and rec.error == "cancelled"


@pytest.mark.asyncio
async def test_failed_run_leaves_a_failed_record() -> None:
    provider = _ScriptThenFail([])  # fails on the very first model call
    agent = Agent(name="bot", model=provider)
    app = _app(agent, retry=RetryPolicy(max_attempts=1))
    store = app.state.store
    async with _client(app) as ac:
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", gone=True)
        await task
        recs = await store.list_runs(session_id="s1")
    assert len(recs) == 1
    rec = await _wait_record(store, recs[0].id)
    assert rec.status == "failed"
    assert rec.error and "scripted non-retryable failure" in rec.error


def test_lifespan_sweeps_stale_running_records(tmp_path) -> None:
    # A record left "running" by a dead process reads as "interrupted" once a
    # new app starts (the lifespan sweep) — not as a phantom live run forever.
    import sqlite3

    from fastapi.testclient import TestClient

    path = tmp_path / "app.db"
    store = ChatStore.sqlite(path)  # constructing it ensures the schema
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO runs (id, session_id, agent, source, status, started_at) "
        "VALUES ('stale', 's1', 'bot', 'user', 'running', 1.0)"
    )
    conn.commit()
    conn.close()

    agent = Agent(name="bot", model=ScriptedProvider([text("hi")]))
    app = _app(agent, store=store)
    with TestClient(app) as c:  # entering the context runs the lifespan
        recs = c.get("/api/runs/history").json()
    assert recs and recs[0]["run_id"] == "stale"
    assert recs[0]["status"] == "interrupted"
