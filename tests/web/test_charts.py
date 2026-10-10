"""``show_chart``: the server-side checks, and parity with the client."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import create_app, show_chart, ui_result  # noqa: E402
from lovia.web.charts import MAX_OPTION_BYTES, SERIES_TYPES  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_web import _app_config, _parse_sse  # noqa: E402

CHARTS_JS = (
    Path(__file__).resolve().parents[2]
    / "lovia"
    / "web"
    / "static"
    / "js"
    / "charts.js"
)
BAR = {
    "dataset": {"source": [["m", "v"], ["Jan", 1], ["Feb", 2]]},
    "xAxis": {"type": "category"},
    "yAxis": {},
    "series": [{"type": "bar"}],
}


def _run(option: object) -> dict:
    """Stream one show_chart call; return its tool_result payload."""
    provider = ScriptedProvider(
        [call("show_chart", {"option": option}, call_id="c1"), text("ok")]
    )
    agent = Agent(name="bot", model=provider, tools=[show_chart])
    app = create_app(agent, generate_titles=False, store=ChatStore.in_memory())
    with TestClient(app).stream(
        "POST", "/api/chat/stream", json={"message": "chart"}
    ) as res:
        events = _parse_sse("".join(res.iter_text()))
    return next(d for e, d in events if e == "tool_result")


def test_a_valid_option_renders_from_the_call_arguments() -> None:
    tr = _run(BAR)
    assert not tr["is_error"]
    # A reference, not a copy: the option already sits in the call arguments.
    assert tr["result"] == ui_result("chart", from_args="option")


def test_a_single_series_object_is_accepted() -> None:
    assert not _run({**BAR, "series": {"type": "line"}})["is_error"]


@pytest.mark.parametrize(
    ("option", "match"),
    [
        ({"xAxis": {}}, "series is missing or empty"),
        ({"series": []}, "series is missing or empty"),
        ({"series": [{"type": "custom"}]}, "'custom' is not supported"),
        ({"series": [{"type": "bar"}, {}]}, r"series\[1\]\.type None"),
        ({"series": ["bar"]}, r"series\[0\]\.type None"),
    ],
)
def test_unusable_options_are_tool_errors(option, match) -> None:
    tr = _run(option)
    assert tr["is_error"]
    assert re.search(match, tr["result"])


def test_an_oversize_option_is_a_tool_error() -> None:
    rows = [["x" * 50, i] for i in range(MAX_OPTION_BYTES // 50)]
    tr = _run({**BAR, "dataset": {"source": rows}})
    assert tr["is_error"]
    assert "the limit is 64 KiB" in tr["result"]


def test_series_allowlist_matches_the_client() -> None:
    source = CHARTS_JS.read_text(encoding="utf-8")
    block = re.search(r"SERIES_TYPES = new Set\(\[(.*?)\]\)", source, re.S)
    assert block, "charts.js should declare SERIES_TYPES"
    assert set(re.findall(r"'(\w+)'", block.group(1))) == SERIES_TYPES


def test_page_config_carries_the_static_prefix() -> None:
    agent = Agent(name="bot", model=ScriptedProvider([]))
    app = create_app(agent, generate_titles=False, store=ChatStore.in_memory())
    page = TestClient(app, root_path="/lovia").get("/").text
    base = _app_config(page)["static_base"]
    assert re.fullmatch(r"/lovia/static/[^/]+/", base)
    # The same prefix the template's own assets use.
    assert f'src="{base}js/main.js"' in page
