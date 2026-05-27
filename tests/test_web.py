"""Tests for the optional ``lovia.web`` module."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent, tool  # noqa: E402
from lovia.web import create_app  # noqa: E402

from .scripted_provider import ScriptedProvider, call, text  # noqa: E402


# ---------------------------------------------------------------- helpers -


def _parse_sse(body: str) -> list[tuple[str, dict | str]]:
    """Parse an SSE response body into ``[(event, data)]`` tuples."""
    body = body.replace("\r\n", "\n")
    events: list[tuple[str, dict | str]] = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        event = "message"
        data_lines: list[str] = []
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        raw = "\n".join(data_lines)
        try:
            events.append((event, json.loads(raw)))
        except json.JSONDecodeError:
            events.append((event, raw))
    return events


def _make_agent(script) -> Agent:
    return Agent(name="bot", model=ScriptedProvider(script))


# ------------------------------------------------------------- basic shape -


def test_healthz_and_index() -> None:
    app = create_app(_make_agent([text("hi")]))
    c = TestClient(app)
    assert c.get("/healthz").json() == {"status": "ok"}
    res = c.get("/")
    assert res.status_code == 200
    assert "<html" in res.text.lower()


def test_list_agents_single() -> None:
    agent = Agent(
        name="writer",
        model=ScriptedProvider([text("hi")]),
        instructions="be helpful",
    )
    c = TestClient(create_app(agent))
    data = c.get("/api/agents").json()
    assert data == [{"name": "writer", "instructions": "be helpful", "tools": []}]


def test_list_agents_multi_and_pick() -> None:
    a = _make_agent([text("a")])
    b = _make_agent([text("b")])
    app = create_app({"alpha": a, "beta": b})
    c = TestClient(app)
    names = sorted(x["name"] for x in c.get("/api/agents").json())
    assert names == ["alpha", "beta"]
    # No agent specified → 400 because multiple are registered.
    bad = c.post("/api/chat", json={"message": "hi"})
    assert bad.status_code == 400
    # Picking by name works.
    ok = c.post("/api/chat", json={"message": "hi", "agent": "alpha"})
    assert ok.json()["output"] == "a"


# -------------------------------------------------------------- chat round -


def test_chat_round_trip_and_usage() -> None:
    c = TestClient(create_app(_make_agent([text("hello world")])))
    res = c.post("/api/chat", json={"message": "hi"}).json()
    assert res["output"] == "hello world"
    assert res["usage"]["total_tokens"] == 2
    assert res["session_id"]


def test_session_persists_across_calls() -> None:
    agent = _make_agent([text("first"), text("second")])
    c = TestClient(create_app(agent))
    sid = c.post("/api/chat", json={"message": "one"}).json()["session_id"]
    c.post("/api/chat", json={"message": "two", "session_id": sid})
    transcript = c.get(f"/api/sessions/{sid}").json()
    roles = [m["role"] for m in transcript]
    assert roles.count("user") == 2
    assert roles.count("assistant") == 2

    c.delete(f"/api/sessions/{sid}")
    assert c.get(f"/api/sessions/{sid}").json() == []


# -------------------------------------------------------------- streaming -


def test_stream_yields_session_text_and_done() -> None:
    c = TestClient(create_app(_make_agent([text("yo")])))
    with c.stream("POST", "/api/chat/stream", json={"message": "hi"}) as res:
        body = "".join(res.iter_text())
    events = _parse_sse(body)
    kinds = [e[0] for e in events]
    assert kinds[0] == "session"
    assert "text_delta" in kinds
    assert kinds[-1] == "done"
    deltas = "".join(e[1]["delta"] for e in events if e[0] == "text_delta")
    assert deltas == "yo"


def test_stream_emits_tool_events() -> None:
    @tool
    async def weather(city: str) -> str:
        """Stub tool."""
        return f"{city}:sunny"

    provider = ScriptedProvider(
        [call("weather", {"city": "paris"}, call_id="c1"), text("done")]
    )
    agent = Agent(name="bot", model=provider, tools=[weather])
    c = TestClient(create_app(agent))
    with c.stream("POST", "/api/chat/stream", json={"message": "go"}) as res:
        events = _parse_sse("".join(res.iter_text()))
    kinds = [e[0] for e in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds


# ---------------------------------------------------------- approval flow -


@pytest.mark.asyncio
async def test_approval_flow_via_http() -> None:
    import asyncio

    import httpx

    @tool(needs_approval=True)
    async def sensitive() -> str:
        """Sensitive."""
        return "did it"

    provider = ScriptedProvider([call("sensitive", {}, call_id="c1"), text("ack")])
    agent = Agent(name="bot", model=provider, tools=[sensitive])
    app = create_app(agent)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        collected: list[str] = []

        async def consume() -> None:
            async with ac.stream(
                "POST",
                "/api/chat/stream",
                json={"message": "go", "session_id": "sess-1"},
            ) as res:
                async for line in res.aiter_lines():
                    collected.append(line)

        consumer = asyncio.create_task(consume())

        # Poll for the approval to be registered, then resolve via HTTP.
        for _ in range(100):
            r = await ac.post(
                "/api/chat/approve",
                json={
                    "session_id": "sess-1",
                    "call_id": "c1",
                    "decision": "approve",
                },
            )
            if r.status_code == 200:
                break
            await asyncio.sleep(0.02)
        else:
            consumer.cancel()
            raise AssertionError("approval was never registered")

        await asyncio.wait_for(consumer, timeout=5)

        body = "\n".join(collected)
        events = _parse_sse(body)
        kinds = [e[0] for e in events]
        assert "approval_required" in kinds
        assert kinds[-1] == "done"

        transcript = (await ac.get("/api/sessions/sess-1")).json()
        tool_msg = next(m for m in transcript if m["role"] == "tool")
        assert tool_msg["content"] == "did it"


def test_unknown_approval_returns_404() -> None:
    c = TestClient(create_app(_make_agent([text("hi")])))
    r = c.post(
        "/api/chat/approve",
        json={"session_id": "nope", "call_id": "nope", "decision": "approve"},
    )
    assert r.status_code == 404
