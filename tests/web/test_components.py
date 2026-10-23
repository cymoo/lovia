"""UI components: the ``ui_result`` envelope and ``ChatUI(scripts=...)``."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent, tool  # noqa: E402
from lovia.exceptions import UserError  # noqa: E402
from lovia.web import ChatUI, create_app, ui_result  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_web import _parse_sse  # noqa: E402


def _app(agent: Agent, **kw):
    return create_app(agent, titles=False, store=ChatStore.in_memory(), **kw)


def test_ui_result_inline_props() -> None:
    out = ui_result("table", {"rows": [["café", 3]]})
    # The client's cheap prefix test relies on this exact opening.
    assert out.startswith('{"lovia_ui"')
    assert "café" in out  # ensure_ascii=False: readable for the model too
    assert json.loads(out) == {
        "lovia_ui": {"kind": "table", "props": {"rows": [["café", 3]]}}
    }


def test_ui_result_null_props_are_props() -> None:
    assert json.loads(ui_result("empty", None)) == {
        "lovia_ui": {"kind": "empty", "props": None}
    }


@pytest.mark.parametrize("ref", [True, "option"])
def test_ui_result_from_args(ref) -> None:
    assert json.loads(ui_result("chart", from_args=ref)) == {
        "lovia_ui": {"kind": "chart", "from_args": ref}
    }


@pytest.mark.parametrize(
    ("args", "kwargs", "match"),
    [
        (("Table", {}), {}, "invalid UI component kind"),
        (("9x", {}), {}, "invalid UI component kind"),
        (("x" * 65, {}), {}, "invalid UI component kind"),
        (("table",), {}, "exactly one"),
        (("table", {}), {"from_args": True}, "exactly one"),
        (("table",), {"from_args": ""}, "invalid from_args"),
        (("table", {"when": object()}), {}, "not JSON-serializable"),
        (("table", {"rows": [[float("nan")]]}), {}, "not JSON-serializable"),
        (("table", {"max": float("inf")}), {}, "not JSON-serializable"),
    ],
)
def test_ui_result_rejects(args, kwargs, match) -> None:
    with pytest.raises(UserError, match=match):
        ui_result(*args, **kwargs)


def test_envelope_reaches_the_client_verbatim() -> None:
    """The envelope travels as the plain tool output — live and in history."""

    @tool
    async def top_products(n: int) -> str:
        """Stub component tool."""
        return ui_result("table", {"rows": [["A1", n]]})

    provider = ScriptedProvider(
        [call("top_products", {"n": 3}, call_id="c1"), text("ok")]
    )
    c = TestClient(_app(Agent(name="bot", model=provider, tools=[top_products])))
    with c.stream("POST", "/api/chat/stream", json={"message": "go"}) as res:
        events = _parse_sse("".join(res.iter_text()))
    expected = ui_result("table", {"rows": [["A1", 3]]})
    assert next(d for e, d in events if e == "tool_result")["result"] == expected

    sid = next(d for e, d in events if e == "session")["session_id"]
    entries = c.get(f"/api/sessions/{sid}").json()["entries"]
    assert next(m for m in entries if m["role"] == "tool")["content"] == expected


def _scripts(page: str) -> list[str]:
    """The extension modules main.js imports before it starts."""
    raw = page.split('<script id="app-config" type="application/json">', 1)[1]
    return json.loads(raw.split("</script>", 1)[0])["scripts"]


def test_scripts_resolve_into_assets_the_app_or_a_url(tmp_path) -> None:
    ui = ChatUI(
        assets=tmp_path,
        scripts=[
            "ext/table.js",
            "/ext/kpi.js",
            "https://cdn.example.com/x.js",
            "//cdn.example.com/y.js",
        ],
    )
    agent = Agent(name="bot", model=ScriptedProvider([]))
    page = TestClient(_app(agent, ui=ui), root_path="/lovia").get("/").text
    assert _scripts(page) == [
        "/lovia/ui/ext/table.js",
        "/lovia/ext/kpi.js",
        "https://cdn.example.com/x.js",
        "//cdn.example.com/y.js",
    ]
    # Imported by main.js, not by tags of their own after it.
    assert "ext/table.js" not in page.split("js/main.js", 1)[1]


def test_scripts_at_the_root_and_as_a_bare_string(tmp_path) -> None:
    agent = Agent(name="bot", model=ScriptedProvider([]))
    ui = ChatUI(assets=tmp_path, scripts="ext/table.js")
    page = TestClient(_app(agent, ui=ui)).get("/").text
    assert _scripts(page) == ["/ui/ext/table.js"]
    assert _scripts(TestClient(_app(agent)).get("/").text) == []


def test_a_file_in_assets_needs_an_assets_directory() -> None:
    with pytest.raises(UserError, match="names a file in assets"):
        ChatUI(scripts=["ext/table.js"])
    with pytest.raises(UserError, match="names a file in assets"):
        ChatUI(logo="logo.svg")
    # A path on the app, or a URL, needs none.
    ChatUI(scripts=["/ext/table.js"], styles=["https://cdn.example.com/a.css"])
