"""ask_human web bridge: QuestionRegistry + POST /api/chat/answer + CLI wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import lovia.web.__main__ as cli
from lovia import Agent
from lovia.exceptions import ToolError
from lovia.plugins import Subagents
from lovia.tools.human import HumanChannel
from lovia.web import QuestionRegistry, RouterDeps, build_api_router, create_app
from lovia.web.approvals import ApprovalRegistry
from lovia.web.store import ChatStore

from ..scripted_provider import ScriptedProvider


# ----------------------------------------------------------- registry unit -
async def _registry(
    channel: HumanChannel, *, timeout: float | None = None
) -> QuestionRegistry:
    registry = QuestionRegistry(channel, timeout=timeout)
    registry.start()
    await asyncio.sleep(0)  # let the consumer task pick up the feed
    return registry


async def test_registry_indexes_by_session_and_resolves() -> None:
    channel = HumanChannel()
    registry = await _registry(channel)
    q, fut = channel._new_question("tea or coffee?", session_id="s1")
    await asyncio.sleep(0)

    assert registry.pending("s1") is not None
    assert registry.pending("s1").id == q.id
    assert registry.pending("nope") is None

    assert registry.resolve("s1", "tea") is True
    assert await asyncio.wait_for(fut, timeout=1) == "tea"
    assert registry.pending("s1") is None
    assert registry.resolve("s1", "again") is False  # nothing left to answer
    await registry.aclose()


async def test_registry_prunes_entries_resolved_behind_its_back() -> None:
    channel = HumanChannel()
    registry = await _registry(channel)
    q, fut = channel._new_question("still there?", session_id="s1")
    await asyncio.sleep(0)

    channel.answer(q.id, "answered elsewhere")  # e.g. a custom operator loop
    assert await fut == "answered elsewhere"
    # The stale index entry must not serve a ghost, nor accept an answer.
    assert registry.pending("s1") is None
    assert registry.resolve("s1", "late") is False
    await registry.aclose()


async def test_registry_timeout_cancels_the_question() -> None:
    channel = HumanChannel()
    registry = await _registry(channel, timeout=0.05)
    _, fut = channel._new_question("anyone?", session_id="s1")
    await asyncio.sleep(0)

    with pytest.raises(ToolError, match="no answer within"):
        await asyncio.wait_for(fut, timeout=1)
    assert registry.pending("s1") is None
    await registry.aclose()


async def test_registry_answer_beats_the_timer() -> None:
    channel = HumanChannel()
    registry = await _registry(channel, timeout=5.0)
    _, fut = channel._new_question("quick!", session_id="s1")
    await asyncio.sleep(0)

    assert registry.resolve("s1", "here") is True
    assert await asyncio.wait_for(fut, timeout=1) == "here"
    await registry.aclose()  # timer must have been dropped with the entry


async def test_registry_cancel_session_fails_the_parked_call() -> None:
    channel = HumanChannel()
    registry = await _registry(channel)
    _, fut = channel._new_question("doomed", session_id="s1")
    await asyncio.sleep(0)

    registry.cancel_session("s1")
    with pytest.raises(ToolError, match="run cancelled"):
        await asyncio.wait_for(fut, timeout=1)
    registry.cancel_session("s1")  # idempotent on an empty session
    await registry.aclose()


async def test_cancel_session_sweeps_questions_the_consumer_has_not_indexed() -> None:
    channel = HumanChannel()
    registry = QuestionRegistry(channel)  # consumer never started: worst case
    _, fut = channel._new_question("just asked", session_id="s1")

    registry.cancel_session("s1")
    with pytest.raises(ToolError, match="run cancelled"):
        await asyncio.wait_for(fut, timeout=1)


async def test_registry_aclose_cancels_parked_calls_and_joins_consumer() -> None:
    channel = HumanChannel()
    registry = await _registry(channel)
    _, fut = channel._new_question("shutdown", session_id="s1")
    await asyncio.sleep(0)

    await registry.aclose()
    with pytest.raises(ToolError, match="shutting down"):
        fut.result()
    # Channel is closed: further questions fail fast.
    with pytest.raises(ToolError, match="closed"):
        channel._new_question("too late")


async def test_registry_replaces_previous_session_entry() -> None:
    channel = HumanChannel()
    registry = await _registry(channel)
    q1, fut1 = channel._new_question("first", session_id="s1")
    await asyncio.sleep(0)
    registry.resolve("s1", "one")
    assert await fut1 == "one"

    q2, _ = channel._new_question("second", session_id="s1")
    await asyncio.sleep(0)
    assert registry.pending("s1").id == q2.id
    assert q2.id != q1.id
    await registry.aclose()


# ------------------------------------------------------------- endpoint ----
async def test_answer_endpoint_resolves_pending_question() -> None:
    channel = HumanChannel()
    registry = await _registry(channel)
    deps = RouterDeps(
        agents={"bot": Agent(name="bot", model=ScriptedProvider([]))},
        store=ChatStore.in_memory(),
        approvals=ApprovalRegistry(),
        questions=registry,
    )
    app = FastAPI()
    app.include_router(build_api_router(deps))

    _, fut = channel._new_question("pick one", session_id="s1")
    await asyncio.sleep(0)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        ok = await ac.post(
            "/api/chat/answer", json={"session_id": "s1", "answer": "that one"}
        )
        assert ok.status_code == 200 and ok.json() == {"ok": True}
        assert await asyncio.wait_for(fut, timeout=1) == "that one"

        # Nothing pending anymore → 404 (the card shows as expired).
        stale = await ac.post(
            "/api/chat/answer", json={"session_id": "s1", "answer": "late"}
        )
        assert stale.status_code == 404
    await registry.aclose()


async def test_answer_endpoint_404_without_question_channel() -> None:
    app = create_app(
        Agent(name="bot", model=ScriptedProvider([])),
        store=ChatStore.in_memory(),
        generate_titles=False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/api/chat/answer", json={"session_id": "s1", "answer": "x"})
        assert r.status_code == 404
        assert "no question channel" in r.json()["detail"]


# ------------------------------------------------------------ CLI wiring ---
def test_build_default_agent_wires_ask_human_parent_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lovia.providers import provider_from_string

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOVIA_MEMORY_DIR", raising=False)
    args = cli.build_parser().parse_args([])
    channel = HumanChannel()
    agent = cli.build_default_agent(
        args,
        ChatStore.in_memory(),
        provider_from_string("test-model"),
        question_channel=channel,
    )
    assert "ask_human" in {t.name for t in agent.tools}
    # The subagent child must not carry it: a delegated background task has
    # no operator watching to answer, so a question would only park it.
    subagents = next(p for p in agent.plugins if isinstance(p, Subagents))
    (child,) = list(subagents.agents)
    assert "ask_human" not in {t.name for t in child.tools}


def test_build_default_agent_without_channel_has_no_ask_human(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lovia.providers import provider_from_string

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOVIA_MEMORY_DIR", raising=False)
    args = cli.build_parser().parse_args([])
    agent = cli.build_default_agent(
        args, ChatStore.in_memory(), provider_from_string("test-model")
    )
    assert "ask_human" not in {t.name for t in agent.tools}
