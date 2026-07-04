"""Tests for the optional ``lovia.web`` module."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent, Todo, tool  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402

_TODOS = {
    "todos": [
        {"content": "Design model", "status": "completed"},
        {
            "content": "Write tests",
            "status": "in_progress",
            "active_form": "Writing tests",
        },
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


def test_context_compacted_sse_forwards_notice() -> None:
    """The compaction SSE payload carries the notice the UI renders."""
    from lovia import events
    from lovia.web.sse import event_to_sse

    ev = events.ContextCompacted(
        session_id="s1",
        entries_before=[],
        entries_after=[],
        notice=events.CompactionNotice(
            reason="reactive_offload+clear",
            reactive=True,
            summary="A running summary.",
            tokens_before=18000,
            tokens_after=9000,
            detail=["context was 82% full", "3 tool results cleared"],
        ),
    )
    payload = event_to_sse(ev)
    assert payload is not None
    assert payload["event"] == "context_compacted"
    data = json.loads(payload["data"])
    assert data["session_id"] == "s1"
    assert data["reactive"] is True
    assert data["summary"] == "A running summary."
    # The notice rides along flat so the UI shows before/after and the
    # policy-authored detail bullets without extra plumbing.
    assert data["tokens_before"] == 18000
    assert data["tokens_after"] == 9000
    assert data["detail"] == ["context was 82% full", "3 tool results cleared"]


def test_session_detail_replays_persisted_compaction_notice() -> None:
    """A finished session surfaces a per-run compaction notice (persisted in the
    segment meta) as a synthetic ``context_compacted`` entry at the run boundary."""
    import asyncio

    from lovia.session import NOTICE_META_KEY
    from lovia.transcript import AssistantTextEntry, InputEntry

    store = ChatStore.in_memory()
    sid = "s-compact"
    notice = {
        "reason": "offload+clear",
        "reactive": False,
        "summary": None,
        "tokens_before": 9000,
        "tokens_after": 5000,
        "detail": ["2 tool results cleared"],
    }
    asyncio.run(
        store.session.append(
            sid,
            [
                InputEntry(role="user", content="hi"),
                AssistantTextEntry(content="hello"),
            ],
            run_id="r1",
            meta={NOTICE_META_KEY: notice},
        )
    )

    c = TestClient(_app(_make_agent([text("x")]), store=store))
    res = c.get(f"/api/sessions/{sid}").json()
    assert [e["role"] for e in res["entries"]] == [
        "user",
        "assistant",
        "context_compacted",
    ]
    out = res["entries"][-1]
    assert out["compaction"]["reason"] == "offload+clear"
    assert out["compaction"]["tokens_before"] == 9000


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
        model=ScriptedProvider(
            [call("todo_write", _TODOS, call_id="c1"), text("done")]
        ),
        plugins=[Todo()],
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


# ---------------------------------------------------------------- export -


def test_export_md_renders_reasoning_as_visible_blockquote() -> None:
    c = TestClient(
        _app(_make_agent([text("the answer", reasoning="step one\nstep two")]))
    )
    c.post("/api/chat", json={"message": "go", "session_id": "s1"})
    body = c.get("/api/sessions/s1/export?format=md").text
    # No collapsed HTML disclosure widget (it would hide reasoning in PDF).
    assert "<details>" not in body
    assert "<summary>" not in body
    # Reasoning is a visible blockquote with the new label, and every line is quoted.
    assert "> **💭 Thinking**" in body
    assert "> step one" in body
    assert "> step two" in body
    # Thinking comes before the answer (the model reasons first), under one heading.
    assert (
        body.index("### Assistant")
        < body.index("💭 Thinking")
        < body.index("the answer")
    )


def test_export_md_quotes_blank_lines_within_reasoning() -> None:
    c = TestClient(_app(_make_agent([text("ok", reasoning="para one\n\npara two")])))
    c.post("/api/chat", json={"message": "go", "session_id": "s1"})
    body = c.get("/api/sessions/s1/export?format=md").text
    # The blank line stays inside the blockquote as a bare `>` so it doesn't break.
    assert "> para one\n>\n> para two" in body


def test_export_md_omits_thinking_block_when_no_reasoning() -> None:
    c = TestClient(_app(_make_agent([text("just an answer")])))
    c.post("/api/chat", json={"message": "go", "session_id": "s1"})
    body = c.get("/api/sessions/s1/export?format=md").text
    assert "💭 Thinking" not in body
    assert "just an answer" in body


def test_export_json_envelope_shape() -> None:
    """The shape the client-side HTML export consumes (export.js)."""
    c = TestClient(_app(_make_agent([text("the answer", reasoning="because")])))
    c.post("/api/chat", json={"message": "go", "session_id": "s1"})
    data = c.get("/api/sessions/s1/export?format=json").json()
    assert set(data) >= {"session_id", "title", "agent", "messages"}
    assert data["session_id"] == "s1"
    msg = data["messages"][-1]  # the assistant turn
    assert set(msg) >= {"role", "content", "reasoning", "tool_calls"}
    assert msg["reasoning"] == "because"
    assert isinstance(msg["tool_calls"], list)


def test_export_attributes_tool_result_to_its_tool() -> None:
    """MD export labels a tool result with its tool's name; JSON carries the link."""

    @tool
    async def weather(city: str) -> str:
        """Stub tool."""
        return f"{city}:sunny"

    provider = ScriptedProvider(
        [call("weather", {"city": "paris"}, call_id="c1"), text("done")]
    )
    c = TestClient(_app(Agent(name="bot", model=provider, tools=[weather])))
    c.post("/api/chat", json={"message": "go", "session_id": "s1"})

    md = c.get("/api/sessions/s1/export?format=md").text
    assert "**Tool: `weather`**" in md  # the call
    assert "Tool result: `weather`" in md  # the result, attributed to its tool
    assert "paris:sunny" in md

    data = c.get("/api/sessions/s1/export?format=json").json()
    # A tool-result message carries tool_call_id linking it back to the call so a
    # consumer (export.js) can label it.
    result_msg = next(m for m in data["messages"] if m["role"] == "tool")
    assert result_msg["tool_call_id"] == "c1"


def test_export_md_does_not_misattribute_tool_result_with_empty_id() -> None:
    """Empty tool-call ids must not collide and mislabel an unrelated result."""
    from lovia.messages import Message, ToolCall
    from lovia.web.api.serialization import export_md

    msgs = [
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="", name="foo", arguments="{}")],
        ),
        Message(role="tool", content="some result", tool_call_id=""),
    ]
    md = export_md(msgs, title="t", session_id="s")
    assert "Tool result: `foo`" not in md  # no real correlation via an empty id
    assert "Tool result" in md  # falls back to the generic label


# ------------------------------------------------------------- pin / patch -


def test_patch_session_pins_and_reorders_list() -> None:
    c = TestClient(_app(_make_agent([text("a"), text("b")])))
    c.post("/api/chat", json={"message": "first", "session_id": "old"})
    c.post("/api/chat", json={"message": "second", "session_id": "new"})  # most recent

    # Unpinned: most recent first.
    ids = [s["id"] for s in c.get("/api/sessions").json()]
    assert ids == ["new", "old"]
    assert all(s["pinned"] is False for s in c.get("/api/sessions").json())

    # Pin the older one → it jumps to the top and reports pinned.
    res = c.patch("/api/sessions/old", json={"pinned": True}).json()
    assert res["pinned"] is True
    ids = [s["id"] for s in c.get("/api/sessions").json()]
    assert ids == ["old", "new"]

    # Unpin → back to recency order.
    c.patch("/api/sessions/old", json={"pinned": False})
    ids = [s["id"] for s in c.get("/api/sessions").json()]
    assert ids == ["new", "old"]


def test_patch_session_rename_still_works() -> None:
    c = TestClient(_app(_make_agent([text("a")])))
    c.post("/api/chat", json={"message": "hi", "session_id": "s1"})
    res = c.patch("/api/sessions/s1", json={"title": "Renamed"}).json()
    assert res["title"] == "Renamed"
    assert res["pinned"] is False


def test_patch_unknown_session_returns_404() -> None:
    c = TestClient(_app(_make_agent([text("a")])))
    assert c.patch("/api/sessions/nope", json={"pinned": True}).status_code == 404


# ----------------------------------------------------------- server info -


def test_info_single_agent_capabilities() -> None:
    c = TestClient(_app(_make_agent([text("hi")])))
    data = c.get("/api/info").json()
    assert data["title"] == "lovia"
    assert data["agents"] == ["bot"]
    assert data["default_agent"] == "bot"
    # ChatStore.in_memory() wires an InMemoryCheckpointer.
    assert data["features"]["checkpointing"] is True
    # _app disables title generation.
    assert data["features"]["titles"] is False


def test_info_multi_agent_has_no_default() -> None:
    app = _app({"alpha": _make_agent([text("a")]), "beta": _make_agent([text("b")])})
    data = TestClient(app).get("/api/info").json()
    assert sorted(data["agents"]) == ["alpha", "beta"]
    assert data["default_agent"] is None


def test_info_reflects_custom_title_and_title_flag() -> None:
    app = _app(_make_agent([text("hi")]), title="My Bot", generate_titles=True)
    data = TestClient(app).get("/api/info").json()
    assert data["title"] == "My Bot"
    assert data["features"]["titles"] is True


def test_get_agent_by_name() -> None:
    agent = Agent(
        name="writer",
        model=ScriptedProvider([text("hi")]),
        instructions="be helpful",
    )
    c = TestClient(_app(agent))
    assert c.get("/api/agents/writer").json() == {
        "name": "writer",
        "instructions": "be helpful",
        "tools": [],
    }
    assert c.get("/api/agents/nope").status_code == 404


# ----------------------------------------------------- delete-all + limit -


def test_delete_all_sessions() -> None:
    c = TestClient(_app(_make_agent([text("a"), text("b")])))
    c.post("/api/chat", json={"message": "one"})
    c.post("/api/chat", json={"message": "two"})
    assert len(c.get("/api/sessions").json()) == 2

    r = c.delete("/api/sessions")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert c.get("/api/sessions").json() == []


def test_sessions_limit_caps_results() -> None:
    c = TestClient(_app(_make_agent([text(str(i)) for i in range(3)])))
    for i in range(3):
        c.post("/api/chat", json={"message": f"m{i}"})
    assert len(c.get("/api/sessions").json()) == 3
    assert len(c.get("/api/sessions?limit=2").json()) == 2


async def test_reconnect_view_does_not_duplicate_user_message() -> None:
    """An interrupted run's user input appears exactly once.

    get_session rebuilds the view as ``session.load() + snapshot.entries``. The
    in-flight input lives only in the checkpoint until the run succeeds, so the
    two halves are disjoint — the user message must not be doubled.
    """
    import httpx

    from lovia.checkpointer import RunHead
    from lovia.messages import Usage
    from lovia.transcript import AssistantTextEntry, InputEntry

    app = _app(_make_agent([text("done")]))
    store: ChatStore = app.state.store
    sid = "sess-reconnect"

    # A prior, already-persisted exchange.
    await store.session.append(
        sid,
        [
            InputEntry(role="user", content="first question"),
            AssistantTextEntry(content="first answer"),
        ],
    )
    await store.upsert(sid, agent="bot")
    # An interrupted run: its own input + partial output live in the checkpoint,
    # not yet in the session.
    await store.checkpointer.append(
        "run-1",
        [
            InputEntry(role="user", content="second question"),
            AssistantTextEntry(content="partial answer"),
        ],
        RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
    )
    await store.set_active_run_id(sid, "run-1")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get(f"/api/sessions/{sid}")
    assert res.status_code == 200
    data = res.json()

    assert data["active_run_id"] == "run-1"
    user_msgs = [e["content"] for e in data["entries"] if e["role"] == "user"]
    assert user_msgs == ["first question", "second question"]  # not doubled
    assert any("partial answer" in str(e["content"]) for e in data["entries"])


# ------------------------------------------------------- API/UI decoupling -


def test_ui_false_serves_api_without_html() -> None:
    app = create_app(
        _make_agent([text("hi")]),
        ui=False,
        store=ChatStore.in_memory(),
        generate_titles=False,
    )
    c = TestClient(app)
    assert c.get("/").status_code == 404  # no bundled chat page
    assert c.get("/static/js/api.js").status_code == 404  # no /static mount
    assert c.get("/api/agents").status_code == 200  # API still works


def test_ui_true_serves_bundled_page_and_static() -> None:
    c = TestClient(_app(_make_agent([text("hi")])))  # ui defaults to True
    page = c.get("/").text
    assert "<html" in page.lower()
    assert "mermaid" in page.lower()  # diagram rendering library is bundled in
    assert c.get("/static/js/api.js").status_code == 200


def test_build_api_router_is_embeddable() -> None:
    from fastapi import FastAPI

    from lovia.web import RouterDeps, build_api_router
    from lovia.web.approvals import ApprovalRegistry

    deps = RouterDeps(
        agents={"bot": _make_agent([text("hi")])},
        store=ChatStore.in_memory(),
        approvals=ApprovalRegistry(),
    )
    app = FastAPI()
    app.include_router(build_api_router(deps))
    c = TestClient(app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/api/agents").json() == [
        {"name": "bot", "instructions": "", "tools": []}
    ]


# --------------------------------------------------- mid-run injection ----


def test_inject_no_active_run_is_not_accepted() -> None:
    c = TestClient(_app(_make_agent([text("hi")])))
    r = c.post("/api/chat/inject", json={"session_id": "nope", "message": "x"})
    assert r.status_code == 200
    assert r.json() == {"accepted": False}


def test_inject_rejects_empty_message() -> None:
    c = TestClient(_app(_make_agent([text("hi")])))
    r = c.post("/api/chat/inject", json={"session_id": "s", "message": "   "})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_inject_mid_run_emits_user_injected_event() -> None:
    import asyncio

    import httpx

    release = asyncio.Event()

    @tool
    async def block() -> str:
        """Block until the test releases the run."""
        await release.wait()
        return "unblocked"

    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("after")])
    agent = Agent(name="bot", model=provider, tools=[block])
    app = _app(agent)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        collected: list[str] = []

        async def consume() -> None:
            async with ac.stream(
                "POST", "/api/chat/stream", json={"message": "go", "session_id": "s1"}
            ) as res:
                async for line in res.aiter_lines():
                    collected.append(line)

        consumer = asyncio.create_task(consume())

        # Wait until turn 1 is actually underway (the model has been called) so
        # the injection is mid-run — an earlier inject would be drained at turn
        # 1's start and land before the tool call.
        for _ in range(250):
            if provider.calls:
                break
            await asyncio.sleep(0.02)
        else:
            consumer.cancel()
            raise AssertionError("run never reached turn 1")
        r = await ac.post(
            "/api/chat/inject", json={"session_id": "s1", "message": "meanwhile"}
        )
        assert r.json().get("accepted")

        release.set()  # let the tool finish → turn 2 drains the injected message
        await asyncio.wait_for(consumer, timeout=5)

    evs = _parse_sse("\n".join(collected))
    kinds = [e[0] for e in evs]
    assert "user_injected" in kinds
    payload = next(d for (e, d) in evs if e == "user_injected")
    assert payload["content"] == "meanwhile"
    # It lands between the first turn's tool call and the run's end.
    assert kinds.index("tool_call") < kinds.index("user_injected") < kinds.index("done")


@pytest.mark.asyncio
async def test_leftover_message_auto_chains_into_next_run() -> None:
    import httpx

    from lovia.messages import Usage
    from lovia.transcript import FinishDelta, TextDelta, UsageDelta

    class ChainProvider:
        name = "chain"
        supports_json_schema = False

        def __init__(self) -> None:
            self.deps = None
            self.sid = "s1"
            self.n = 0

        async def stream(
            self, entries, *, tools=None, response_format=None, settings=None
        ):
            self.n += 1
            if self.n == 1:
                # Pushed after this run's turn-start drain → a leftover that
                # seeds the next run over the same connection.
                self.deps.mailboxes[self.sid].push("again")
                yield TextDelta(text="first")
            else:
                yield TextDelta(text="second")
            yield UsageDelta(usage=Usage(input_tokens=1, output_tokens=1))
            yield FinishDelta(reason="stop")

    provider = ChainProvider()
    app = _app(Agent(name="bot", model=provider))
    provider.deps = app.state.deps

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        collected: list[str] = []
        async with ac.stream(
            "POST", "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        ) as res:
            async for line in res.aiter_lines():
                collected.append(line)

    evs = _parse_sse("\n".join(collected))
    kinds = [e[0] for e in evs]
    # Two runs over one connection: two `done`, a single `session` envelope.
    assert kinds.count("done") == 2
    assert kinds.count("session") == 1
    deltas = "".join(d["delta"] for (e, d) in evs if e == "text_delta")
    assert deltas == "firstsecond"
    # The leftover reaches the next run as a rendered user turn (not silent input).
    injected = [d["content"] for (e, d) in evs if e == "user_injected"]
    assert injected == ["again"]
    # The mailbox was torn down once the chain finished.
    assert "s1" not in app.state.deps.mailboxes


@pytest.mark.asyncio
async def test_inject_then_cancel_does_not_chain() -> None:
    import asyncio

    import httpx

    release = asyncio.Event()

    @tool
    async def block() -> str:
        """Block until released."""
        await release.wait()
        return "ok"

    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("after")])
    agent = Agent(name="bot", model=provider, tools=[block])
    app = _app(agent)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        collected: list[str] = []

        async def consume() -> None:
            async with ac.stream(
                "POST", "/api/chat/stream", json={"message": "go", "session_id": "s1"}
            ) as res:
                async for line in res.aiter_lines():
                    collected.append(line)

        consumer = asyncio.create_task(consume())
        for _ in range(100):
            r = await ac.post(
                "/api/chat/inject", json={"session_id": "s1", "message": "queued"}
            )
            if r.json().get("accepted"):
                break
            await asyncio.sleep(0.02)
        else:
            consumer.cancel()
            raise AssertionError("inject was never accepted")

        # Cancel instead of releasing the tool: the run stops, no next run.
        await ac.post("/api/chat/cancel", params={"session_id": "s1"})
        release.set()
        await asyncio.wait_for(consumer, timeout=5)

    # Only the first turn ran; the mailbox was cleaned up; no second run.
    assert len(provider.calls) == 1
    assert "s1" not in app.state.deps.mailboxes


@pytest.mark.asyncio
async def test_uninject_withdraws_a_queued_message() -> None:
    import asyncio

    import httpx

    release = asyncio.Event()

    @tool
    async def block() -> str:
        """Block until released."""
        await release.wait()
        return "ok"

    provider = ScriptedProvider([call("block", {}, call_id="c1"), text("after")])
    agent = Agent(name="bot", model=provider, tools=[block])
    app = _app(agent)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        collected: list[str] = []

        async def consume() -> None:
            async with ac.stream(
                "POST", "/api/chat/stream", json={"message": "go", "session_id": "s1"}
            ) as res:
                async for line in res.aiter_lines():
                    collected.append(line)

        consumer = asyncio.create_task(consume())
        inj_id = None
        for _ in range(100):
            d = (
                await ac.post(
                    "/api/chat/inject", json={"session_id": "s1", "message": "oops"}
                )
            ).json()
            if d.get("accepted"):
                inj_id = d["id"]
                break
            await asyncio.sleep(0.02)
        else:
            consumer.cancel()
            raise AssertionError("inject was never accepted")

        # Withdraw it before the run drains it.
        r = await ac.post("/api/chat/uninject", json={"session_id": "s1", "id": inj_id})
        assert r.json() == {"removed": True}

        release.set()  # turn 2 runs with nothing queued
        await asyncio.wait_for(consumer, timeout=5)

    evs = _parse_sse("\n".join(collected))
    assert "user_injected" not in [e[0] for e in evs]
    # The model never saw the withdrawn message.
    assert all("oops" not in str(m.content) for msgs in provider.calls for m in msgs)


# ----------------------------------------------------------- phase-1 fixes -


def test_chat_rejects_empty_message() -> None:
    c = TestClient(_app(_make_agent([text("hi")])))
    assert c.post("/api/chat", json={"message": "   "}).status_code == 422
    # Stream: an empty message with no live run to attach to is also a 422 —
    # and must not leave an empty "New chat" row behind.
    assert c.post("/api/chat/stream", json={"message": ""}).status_code == 422
    assert c.get("/api/sessions").json() == []


def test_coerce_handles_datetime_fields_in_structured_output() -> None:
    import datetime as dt
    import json as _json

    from pydantic import BaseModel

    from lovia.web.sse import _coerce

    class Report(BaseModel):
        title: str
        due: dt.datetime

    out = _coerce(Report(title="x", due=dt.datetime(2026, 7, 5, 12, 0)))
    # Must round-trip through json.dumps — the SSE `done` event depends on it.
    assert _json.loads(_json.dumps(out)) == {"title": "x", "due": "2026-07-05T12:00:00"}
