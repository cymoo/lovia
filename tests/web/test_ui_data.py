"""Component data: ``put_ui_data`` keeps a component's full props out of the
model's context, in the chat store, served by ``/api/sessions/{id}/ui-data``."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent, tool  # noqa: E402
from lovia.exceptions import ToolError, UserError  # noqa: E402
from lovia.web import create_app, put_ui_data, ui_result  # noqa: E402
from lovia.web import components  # noqa: E402
from lovia.web.components import UI_DATA_SINK  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_web import _parse_sse  # noqa: E402

FULL = {"columns": ["n"], "rows": [[i] for i in range(100)]}
PREVIEW = {"columns": ["n"], "rows": [[0], [1]], "row_count": 100}


@tool
async def query() -> str:
    """Stub data tool: a preview for the model, the full rows for the page."""
    return ui_result("table", PREVIEW, data=await put_ui_data(FULL))


def _client(store: ChatStore, **kw) -> TestClient:
    provider = ScriptedProvider([call("query", {}, call_id="c1"), text("done")])
    agent = Agent(name="bot", model=provider, tools=[query])
    return TestClient(create_app(agent, titles=False, store=store, **kw))


def _ref(output: str) -> str:
    return json.loads(output)["lovia_ui"]["data"]


# ---- the Python API ------------------------------------------------------


async def test_outside_a_web_run_there_is_no_store_and_no_ref() -> None:
    assert await put_ui_data(FULL) is None
    assert json.loads(ui_result("table", PREVIEW, data=None)) == {
        "lovia_ui": {"kind": "table", "props": PREVIEW}
    }


def test_ui_result_carries_a_ref_beside_the_preview() -> None:
    ref = "A" * 22
    assert json.loads(ui_result("table", PREVIEW, data=ref)) == {
        "lovia_ui": {"kind": "table", "props": PREVIEW, "data": ref}
    }


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"from_args": True, "data": "A" * 22}, "goes with `props`"),
        ({"props": PREVIEW, "data": "short"}, "invalid UI data ref"),
        ({"props": PREVIEW, "data": "a/b" * 8}, "invalid UI data ref"),
    ],
)
def test_ui_result_rejects_a_bad_ref(kwargs, match) -> None:
    with pytest.raises(UserError, match=match):
        ui_result("table", **kwargs)


async def test_put_ui_data_checks_what_the_page_can_parse(monkeypatch) -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1")
    token = UI_DATA_SINK.set((store, "s1"))
    try:
        with pytest.raises(ToolError, match="NaN"):
            await put_ui_data({"v": float("nan")})
        monkeypatch.setattr(components, "MAX_UI_DATA_BYTES", 1024)
        with pytest.raises(ToolError, match="the limit is 1 KiB") as exc:
            await put_ui_data({"rows": ["x" * 2000]})
        assert "fewer rows" in (exc.value.hint or "")
        ref = await put_ui_data(FULL)
    finally:
        UI_DATA_SINK.reset(token)
    assert ref is not None
    assert json.loads(await store.get_ui_data("s1", ref) or b"") == FULL


# ---- the store -------------------------------------------------------------


async def test_store_scopes_refs_to_their_chat_and_deletes_with_it() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1")
    await store.upsert("s2")
    a = await store.put_ui_data("s1", b'{"x": 1}')
    b = await store.put_ui_data("s2", b'{"x": 2}')
    assert a and b and a != b
    assert await store.get_ui_data("s1", a) == b'{"x": 1}'
    assert await store.get_ui_data("s2", a) is None  # another chat's ref
    await store.delete("s1")
    assert await store.get_ui_data("s1", a) is None
    assert await store.get_ui_data("s2", b) == b'{"x": 2}'
    await store.delete_all()
    assert await store.get_ui_data("s2", b) is None


async def test_a_deleted_chat_takes_no_more_data() -> None:
    """A run cancelled by the delete winds down later; its tool must not
    leave rows behind the delete."""
    store = ChatStore.in_memory()
    await store.upsert("s1")
    await store.delete("s1")
    assert await store.put_ui_data("s1", b'{"x": 1}') is None
    assert await store.put_ui_data("never-created", b'{"x": 1}') is None
    token = UI_DATA_SINK.set((store, "s1"))
    try:
        assert await put_ui_data(FULL) is None  # the tool degrades to its preview
    finally:
        UI_DATA_SINK.reset(token)


# ---- through the app ---------------------------------------------------------


def test_streamed_run_stores_the_data_and_the_route_serves_it() -> None:
    store = ChatStore.in_memory()
    c = _client(store)
    with c.stream("POST", "/api/chat/stream", json={"message": "go"}) as res:
        events = _parse_sse("".join(res.iter_text()))
    sid = next(d for e, d in events if e == "session")["session_id"]
    output = next(d for e, d in events if e == "tool_result")["result"]
    assert json.loads(output)["lovia_ui"]["props"] == PREVIEW  # all the model saw
    ref = _ref(output)

    res = c.get(f"/api/sessions/{sid}/ui-data/{ref}")
    assert res.status_code == 200
    assert res.json() == FULL
    assert "immutable" in res.headers["cache-control"]

    other = c.get(f"/api/sessions/someone-else/ui-data/{ref}")
    assert other.status_code == 404
    assert other.json()["detail"]["code"] == "ui_data_not_found"

    assert c.delete(f"/api/sessions/{sid}").status_code == 200
    assert c.get(f"/api/sessions/{sid}/ui-data/{ref}").status_code == 404


def test_plain_chat_endpoint_stores_the_data_too() -> None:
    c = _client(ChatStore.in_memory())
    res = c.post("/api/chat", json={"message": "go"}).json()
    sid = res["session_id"]
    entries = c.get(f"/api/sessions/{sid}").json()["entries"]
    output = next(m for m in entries if m["role"] == "tool")["content"]
    assert c.get(f"/api/sessions/{sid}/ui-data/{_ref(output)}").json() == FULL


def test_route_is_behind_the_token_guard() -> None:
    c = _client(ChatStore.in_memory(), token="sesame-open")
    assert c.get(f"/api/sessions/s1/ui-data/{'A' * 22}").status_code == 401
