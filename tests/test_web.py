"""Tests for the optional ``lovia.web`` module."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent, tool, todo_plugin  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from .scripted_provider import ScriptedProvider, call, text  # noqa: E402

_TODOS = {
    "todos": [
        {"content": "Design model", "status": "completed"},
        {"content": "Write tests", "status": "in_progress", "active_form": "Writing tests"},
        {"content": "Document", "status": "pending"},
    ]
}


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


def _app(agent_or_agents, **kw):
    """Convenience: tests don't need title-gen polluting their scripts."""
    kw.setdefault("generate_titles", False)
    kw.setdefault("store", ChatStore.in_memory())
    return create_app(agent_or_agents, **kw)


# ------------------------------------------------------------- basic shape -


def test_healthz_and_index() -> None:
    app = _app(_make_agent([text("hi")]))
    c = TestClient(app)
    assert c.get("/healthz").json() == {"status": "ok"}
    res = c.get("/")
    assert res.status_code == 200
    assert "<html" in res.text.lower()
    assert "Wake up, Neo." in res.text
    assert "The Matrix has you." in res.text


def test_index_accepts_custom_empty_state() -> None:
    app = _app(
        _make_agent([text("hi")]),
        empty_title="Mission control",
        empty_description=["Tune the array", "Listen for the reply"],
    )
    res = TestClient(app).get("/")
    assert res.status_code == 200
    assert "Mission control" in res.text
    assert "Tune the array" in res.text
    assert "Listen for the reply" in res.text


def test_list_agents_single() -> None:
    agent = Agent(
        name="writer",
        model=ScriptedProvider([text("hi")]),
        instructions="be helpful",
    )
    c = TestClient(_app(agent))
    data = c.get("/api/agents").json()
    assert data == [{"name": "writer", "instructions": "be helpful", "tools": []}]


def test_markdown_endpoint_renders_and_escapes_html() -> None:
    c = TestClient(_app(_make_agent([text("hi")])))
    res = c.post(
        "/api/markdown",
        json={"text": "**bold**\n\n<script>alert(1)</script>"},
    )
    assert res.status_code == 200
    html = res.json()["html"]
    assert "<strong>bold</strong>" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_list_agents_multi_and_pick() -> None:
    a = _make_agent([text("a")])
    b = _make_agent([text("b")])
    app = _app({"alpha": a, "beta": b})
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
    c = TestClient(_app(_make_agent([text("hello world")])))
    res = c.post("/api/chat", json={"message": "hi"}).json()
    assert res["output"] == "hello world"
    assert res["usage"]["total_tokens"] == 2
    assert res["session_id"]


def test_session_persists_across_calls() -> None:
    agent = _make_agent([text("first"), text("second")])
    c = TestClient(_app(agent))
    sid = c.post("/api/chat", json={"message": "one"}).json()["session_id"]
    c.post("/api/chat", json={"message": "two", "session_id": sid})
    transcript = c.get(f"/api/sessions/{sid}").json()["entries"]
    roles = [m["role"] for m in transcript]
    assert roles.count("user") == 2
    assert roles.count("assistant") == 2

    c.delete(f"/api/sessions/{sid}")
    assert c.get(f"/api/sessions/{sid}").json()["entries"] == []


# -------------------------------------------------------------- streaming -


def test_stream_yields_session_text_and_done() -> None:
    c = TestClient(_app(_make_agent([text("yo")])))
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
    c = TestClient(_app(agent))
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
    app = _app(agent)
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

        transcript = (await ac.get("/api/sessions/sess-1")).json()["entries"]
        tool_msg = next(m for m in transcript if m["role"] == "tool")
        assert tool_msg["content"] == "did it"


def test_unknown_approval_returns_404() -> None:
    c = TestClient(_app(_make_agent([text("hi")])))
    r = c.post(
        "/api/chat/approve",
        json={"session_id": "nope", "call_id": "nope", "decision": "approve"},
    )
    assert r.status_code == 404


# --------------------------------- ApprovalRegistry direct unit tests -----


async def _make_approval_event(name: str = "do_it") -> object:
    """Build a minimal ApprovalRequired event for registry testing."""
    from lovia import events
    from lovia.messages import ToolCall

    loop = __import__("asyncio").get_running_loop()
    decision_fut: __import__("asyncio").Future[bool] = loop.create_future()

    class _Ev(events.ApprovalRequired):
        def __init__(self) -> None:
            super().__init__(call=ToolCall(id="c1", name=name, arguments={}))

        def approve(self) -> None:  # type: ignore[override]
            if not decision_fut.done():
                decision_fut.set_result(True)

        def reject(self) -> None:  # type: ignore[override]
            if not decision_fut.done():
                decision_fut.set_result(False)

    return _Ev(), decision_fut


async def test_approval_registry_resolves() -> None:
    import asyncio

    from lovia.web.approvals import ApprovalRegistry

    reg = ApprovalRegistry()
    ev, verdict = await _make_approval_event()

    waiter = asyncio.create_task(reg.await_decision("s", ev))
    await asyncio.sleep(0)  # let waiter register
    assert await reg.resolve("s", "c1", True) is True
    assert await waiter is True
    assert verdict.result() is True


async def test_approval_registry_cancellation_default_denies() -> None:
    import asyncio

    from lovia.web.approvals import ApprovalRegistry

    reg = ApprovalRegistry()
    ev, verdict = await _make_approval_event()

    waiter = asyncio.create_task(reg.await_decision("s", ev))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    # The event must have been rejected so the runner doesn't hang.
    assert verdict.result() is False


async def test_approval_registry_release_default_denies() -> None:
    import asyncio

    from lovia.web.approvals import ApprovalRegistry

    reg = ApprovalRegistry()
    ev, verdict = await _make_approval_event()

    waiter = asyncio.create_task(reg.await_decision("s", ev))
    await asyncio.sleep(0)
    await reg.release("s")
    assert await waiter is False
    assert verdict.result() is False


async def test_approval_registry_resolve_unknown() -> None:
    from lovia.web.approvals import ApprovalRegistry

    reg = ApprovalRegistry()
    assert await reg.resolve("nope", "nope", True) is False


# ------------------------------------------------------------------ todos -


def _todo_agent() -> Agent:
    return Agent(
        name="bot",
        model=ScriptedProvider([call("todo_write", _TODOS, call_id="c1"), text("done")]),
        plugins=[todo_plugin()],
    )


def test_stream_emits_todo_event_and_suppresses_tool_result() -> None:
    client = TestClient(_app(_todo_agent()))
    res = client.post("/api/chat/stream", json={"message": "go"})
    evs = _parse_sse(res.text)

    todos = [d for (e, d) in evs if e == "todo"]
    assert todos, "expected a todo event"
    payload = todos[0]
    assert payload["name"] == "todo_write"
    assert [t["content"] for t in payload["todos"]] == [
        "Design model",
        "Write tests",
        "Document",
    ]
    assert payload["todos"][1]["active_form"] == "Writing tests"
    # The structured todo event replaces the raw tool_result for that call.
    assert all(d.get("name") != "todo_write" for (e, d) in evs if e == "tool_result")


def test_todos_api_reconstructs_latest_from_session() -> None:
    client = TestClient(_app(_todo_agent()))
    client.post("/api/chat/stream", json={"message": "go", "session_id": "s1"})

    res = client.get("/api/sessions/s1/todos")
    assert res.status_code == 200
    data = res.json()
    assert [t["content"] for t in data["todos"]] == [
        "Design model",
        "Write tests",
        "Document",
    ]
    assert data["todos"][0]["status"] == "completed"


def test_todos_api_empty_for_session_without_todos() -> None:
    client = TestClient(_app(_make_agent([text("hi")])))
    client.post("/api/chat/stream", json={"message": "go", "session_id": "s2"})
    res = client.get("/api/sessions/s2/todos")
    assert res.status_code == 200
    assert res.json()["todos"] == []


def test_max_turns_caps_the_agent_loop() -> None:
    @tool
    async def noop() -> str:
        """A tool that never ends the loop on its own."""
        return "ok"

    # The script would keep calling the tool forever; max_turns must stop it.
    provider = ScriptedProvider([call("noop", {}, call_id=f"c{i}") for i in range(6)])
    agent = Agent(name="bot", model=provider, tools=[noop])
    client = TestClient(_app(agent, max_turns=2))
    res = client.post("/api/chat/stream", json={"message": "go"})
    evs = _parse_sse(res.text)
    assert len(provider.calls) <= 2
    # The cap surfaces as a clean `error` event, not a faulted response.
    assert any(e == "error" for (e, _) in evs)
