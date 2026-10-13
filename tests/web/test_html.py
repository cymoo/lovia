"""``show_html``: the server-side checks, its envelope, and the chat page's
``frame-src`` policy that keeps a sandboxed page from navigating its data away."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import create_app, show_html, ui_result  # noqa: E402
from lovia.web.html import MAX_HTML_BYTES  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_web import _parse_sse  # noqa: E402

PAGE = "<button onclick=\"lovia.send('more')\">More</button><script>1</script>"


def _run(args: dict) -> dict:
    """Stream one show_html call; return its tool_result payload."""
    provider = ScriptedProvider([call("show_html", args, call_id="c1"), text("ok")])
    agent = Agent(name="bot", model=provider, tools=[show_html])
    app = create_app(agent, generate_titles=False, store=ChatStore.in_memory())
    with TestClient(app).stream(
        "POST", "/api/chat/stream", json={"message": "go"}
    ) as res:
        events = _parse_sse("".join(res.iter_text()))
    return next(d for e, d in events if e == "tool_result")


def test_a_page_renders_from_the_call_arguments() -> None:
    result = _run({"html": PAGE, "title": "Explorer"})
    assert not result["is_error"]
    assert result["result"] == ui_result("html", from_args=True)


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"html": "  \n"}, "html is empty"),
        ({"html": "x" * (MAX_HTML_BYTES + 1)}, "the limit is 128 KiB"),
    ],
)
def test_bad_pages_come_back_as_tool_errors(args: dict, match: str) -> None:
    result = _run(args)
    assert result["is_error"] and match in result["result"]


def test_the_chat_page_forbids_frame_navigation() -> None:
    agent = Agent(name="bot", model=ScriptedProvider([]))
    app = create_app(agent, generate_titles=False, store=ChatStore.in_memory())
    res = TestClient(app).get("/")
    assert res.headers["content-security-policy"] == "frame-src 'none'"
