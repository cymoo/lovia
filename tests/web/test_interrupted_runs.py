"""A run cut off by a limit or a failure is still the chat's run: the next
message continues it, with its limits counted afresh, and opening the chat
doesn't resume it on its own."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from lovia import Agent, tool  # noqa: E402
from lovia.checkpointer import RunSnapshot  # noqa: E402
from lovia.exceptions import ProviderError  # noqa: E402
from lovia.messages import Usage  # noqa: E402
from lovia.plugins.todo import Todo  # noqa: E402
from lovia.reliability import RetryPolicy, RunBudget  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402
from lovia.web.supervisor import _counted_from  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_supervisor import _client, _wait_run  # noqa: E402
from .test_web import _app, _parse_sse  # noqa: E402

TASK = "do the 3-step task"


class _Flaky(ScriptedProvider):
    """A script that may hold an exception: raised in that call's place."""

    def _pop(self, input):  # type: ignore[no-untyped-def]
        turn = super()._pop(input)
        if isinstance(turn, BaseException):
            raise turn
        return turn


def _gateway_down() -> ProviderError:
    err = ProviderError("upstream gateway restarting")
    err.status_code, err.retryable = 502, True
    return err


def _todos(*states: str) -> dict:
    return {
        "todos": [
            {"content": f"step {i + 1}", "status": s} for i, s in enumerate(states)
        ]
    }


async def _stream(ac, path, **kw) -> list[tuple[str, dict]]:
    lines: list[str] = []
    async with ac.stream("POST", path, **kw) as res:
        async for line in res.aiter_lines():
            lines.append(line)
    return _parse_sse("\n".join(lines))


def _saw(messages, needle: str) -> bool:
    """Whether the conversation (not the system prompt) holds ``needle``."""
    return any(
        needle in json.dumps(m.__dict__, default=str, ensure_ascii=False)
        for m in messages
        if m.role != "system"
    )


def _outage_agent(after: list) -> tuple[Agent, _Flaky]:
    provider = _Flaky(
        [
            call("todo_write", _todos("in_progress", "pending", "pending")),
            call("todo_write", _todos("completed", "in_progress", "pending")),
            _gateway_down(),
            *after,
        ]
    )
    return Agent(name="bot", model=provider, plugins=[Todo()]), provider


async def _cut_by_outage(ac, store) -> str:
    """Run until step 2 is in progress, then lose the gateway."""
    evs = await _stream(
        ac, "/api/chat/stream", json={"message": TASK, "session_id": "s1"}
    )
    await _wait_run(ac, "s1", gone=True)
    assert [e for e, _ in evs][-1] == "error"
    rid = await store.get_active_run_id("s1")
    snap = await store.checkpointer.load(rid)
    assert snap is not None and snap.status == "interrupted"
    return rid


async def test_a_message_continues_a_run_cut_off_by_an_outage() -> None:
    """User A: "continue" after the gateway came back picks the run up at
    step 2 — the model sees the task and its todo progress, once."""
    agent, provider = _outage_agent([text("carrying on with step 2")])
    store = ChatStore.in_memory()
    app = _app(agent, store=store, retry=RetryPolicy(max_attempts=1))
    async with _client(app) as ac:
        rid = await _cut_by_outage(ac, store)
        # Opening the chat shows a notice, not a reconnect pointer.
        detail = (await ac.get("/api/sessions/s1")).json()
        assert detail["active_run_id"] is None
        assert detail["waiting"]["kind"] == "message"
        assert detail["waiting"]["reason"] == "error"
        assert "gateway" in detail["waiting"]["error"]

        evs = await _stream(
            ac, "/api/chat/stream", json={"message": "继续", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", gone=True)
    kinds = [e for e, _ in evs]
    assert kinds[-1] == "done"
    assert "snapshot" in kinds and "user_injected" in kinds
    seen = provider.calls[-1]
    assert _saw(seen, "todo_write") and _saw(seen, "[~] step 2")  # its progress
    users = [m.content for m in seen if m.role == "user"]
    assert users == [TASK, "继续"]  # the task once, then the message
    assert await store.checkpointer.load(rid) is None  # finished into the chat
    kept = repr(await store.session.load("s1"))
    assert "todo_write" in kept and "carrying on" in kept


async def test_a_reconnect_does_not_resume_a_run_cut_off_by_an_outage() -> None:
    agent, provider = _outage_agent([])
    store = ChatStore.in_memory()
    app = _app(agent, store=store, retry=RetryPolicy(max_attempts=1))
    async with _client(app) as ac:
        rid = await _cut_by_outage(ac, store)
        calls = len(provider.calls)
        res = await ac.post("/api/chat/reconnect", params={"session_id": "s1"})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "run_interrupted"
    assert len(provider.calls) == calls  # nothing spent
    assert await store.checkpointer.load(rid) is not None


@tool
async def noop() -> str:
    """Do nothing."""
    return "ok"


async def test_a_message_continues_a_run_cut_off_by_max_turns() -> None:
    """User B: the message continues *this* chat's run with fresh turns, even
    under the limit it hit — no restart, no raised limit needed."""
    store = ChatStore.in_memory()
    provider = ScriptedProvider(
        [
            call("noop", {}, call_id="c1"),
            call("noop", {}, call_id="c2"),
            call("noop", {}, call_id="c3"),
            text("finished"),
        ]
    )
    app = _app(
        Agent(name="bot", model=provider, tools=[noop]), store=store, max_turns=2
    )
    async with _client(app) as ac:
        evs = await _stream(
            ac, "/api/chat/stream", json={"message": "long task", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", gone=True)
        assert [d["code"] for e, d in evs if e == "error"] == ["max_turns"]
        detail = (await ac.get("/api/sessions/s1")).json()
        assert detail["waiting"]["reason"] == "max_turns"
        assert detail["active_run_id"] is None

        evs = await _stream(
            ac, "/api/chat/stream", json={"message": "go on", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", gone=True)
    assert [e for e, _ in evs][-1] == "done"
    seen = provider.calls[-1]
    assert _saw(seen, "c1") and _saw(seen, "c3")  # the same run, carried on
    assert [m.content for m in seen if m.role == "user"] == ["long task", "go on"]


async def test_a_run_cut_off_by_a_shutdown_still_reconnects() -> None:
    """A shutdown's cut is not the person's to answer: the pointer stays, and
    opening the chat resumes the run as before."""
    from lovia.checkpointer import RunHead
    from lovia.transcript import InputEntry

    store = ChatStore.in_memory()
    await store.upsert("s1", agent="bot")
    await store.checkpointer.append(
        "run-1",
        [InputEntry(role="user", content="q")],
        RunHead(
            agent_name="bot",
            usage=Usage(),
            turns=1,
            status="interrupted",
            error={"type": "RunCancelled", "message": "server shutting down"},
        ),
    )
    await store.set_active_run_id("s1", "run-1")
    app = _app(
        Agent(name="bot", model=ScriptedProvider([text("resumed")])), store=store
    )
    async with _client(app) as ac:
        detail = (await ac.get("/api/sessions/s1")).json()
        assert detail["active_run_id"] == "run-1" and detail["waiting"] is None
        evs = await _stream(ac, "/api/chat/reconnect", params={"session_id": "s1"})
    assert [e for e, _ in evs][-1] == "done"


def test_a_continued_runs_budget_counts_from_where_it_stopped() -> None:
    snap = RunSnapshot(
        run_id="r",
        agent_name="a",
        entries=[],
        usage=Usage(input_tokens=100, output_tokens=20),
        turns=3,
        tool_calls=4,
        elapsed=7.5,
    )
    budget = RunBudget(
        max_input_tokens=1000,
        max_total_tokens=None,
        max_tool_calls=5,
        max_seconds=60,
    )
    fresh = _counted_from(budget, snap)
    assert fresh is not None and fresh is not budget
    assert fresh.max_input_tokens == 1100
    assert fresh.max_output_tokens is None and fresh.max_total_tokens is None
    assert fresh.max_tool_calls == 9
    assert fresh.max_seconds == 67.5
    assert _counted_from(None, snap) is None
