"""``show_table``: the server-side checks, and the envelope it returns."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import create_app, show_table, ui_result  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402
from lovia.web.tables import MAX_TABLE_BYTES  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_web import _parse_sse  # noqa: E402

TABLE = {
    "title": "Cities",
    "columns": ["city", "population", "capital"],
    "rows": [
        ["Tokyo", 37400068, True],
        ["Osaka", 19281000, False],
        ["Nara", None, None],
    ],
}


def _run(args: dict) -> dict:
    """Stream one show_table call; return its tool_result payload."""
    provider = ScriptedProvider([call("show_table", args, call_id="c1"), text("ok")])
    agent = Agent(name="bot", model=provider, tools=[show_table])
    app = create_app(agent, titles=False, store=ChatStore.in_memory())
    with TestClient(app).stream(
        "POST", "/api/chat/stream", json={"message": "table"}
    ) as res:
        events = _parse_sse("".join(res.iter_text()))
    return next(d for e, d in events if e == "tool_result")


def test_a_valid_table_renders_from_the_call_arguments() -> None:
    tr = _run(TABLE)
    assert not tr["is_error"]
    # A reference, not a copy: the table already sits in the call arguments.
    assert tr["result"] == ui_result("table", from_args=True)


def test_the_title_is_optional() -> None:
    assert not _run({"columns": ["a"], "rows": [[1]]})["is_error"]


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"columns": [], "rows": []}, "columns is empty"),
        (
            {"columns": ["a", "b"], "rows": [[1, 2], [3]]},
            r"rows\[1\] has 1 values; there are 2",
        ),
        ({"columns": ["a"], "rows": [[float("nan")]]}, "NaN or Infinity"),
    ],
)
def test_unusable_tables_are_tool_errors(args, match) -> None:
    tr = _run(args)
    assert tr["is_error"]
    assert re.search(match, tr["result"])


def test_a_non_scalar_cell_is_rejected_by_the_schema() -> None:
    assert _run({"columns": ["a"], "rows": [[{"x": 1}]]})["is_error"]


def test_an_oversize_table_is_a_tool_error() -> None:
    rows = [["x" * 50] for _ in range(MAX_TABLE_BYTES // 50)]
    tr = _run({"columns": ["a"], "rows": rows})
    assert tr["is_error"]
    assert "the limit is 64 KiB" in tr["result"]
