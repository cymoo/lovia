"""Web search over a hosted search MCP server (lovia/web/search.py)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from lovia.exceptions import ToolError  # noqa: E402
from lovia.plugins.mcp import MCPServerStreamableHTTP, MCPToolResult  # noqa: E402
from lovia.web.config.schema import SearchConfig  # noqa: E402
from lovia.web.search import PRESETS, compact_results, search_tool  # noqa: E402


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fake search server: ``answers`` queued by the test, ``calls`` as it
    saw them."""
    fake = SimpleNamespace(answers=[], calls=[])

    @asynccontextmanager
    async def session(self: MCPServerStreamableHTTP):  # type: ignore[no-untyped-def]
        async def call(name: str, args: dict[str, Any]) -> MCPToolResult:
            fake.calls.append(
                {"url": self.url, "headers": self.headers, "tool": name, "args": args}
            )
            return fake.answers.pop(0)

        yield SimpleNamespace(call=call)

    monkeypatch.setattr(MCPServerStreamableHTTP, "session", session)
    return fake


async def _search(search: SearchConfig, **args: Any) -> Any:
    tool = search_tool(search)
    assert tool is not None
    return await tool.invoke(args, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("provider", "tool", "args"),
    [
        ("parallel", "web_search", {"objective": "q", "search_queries": ["q"]}),
        (
            "tavily",
            "tavily_search",
            {"query": "q", "max_results": 3, "time_range": "week"},
        ),
        ("exa", "web_search_exa", {"query": "q", "objective": "q", "numResults": 3}),
    ],
)
async def test_one_tool_shape_for_every_service(
    server: SimpleNamespace, provider: str, tool: str, args: dict[str, Any]
) -> None:
    server.answers.append(
        MCPToolResult(content=[_text("Title: A\nURL: https://a.example")])
    )
    out = await _search(
        SearchConfig(provider=provider), query="q", max_results=3, time_range="w"
    )
    call = server.calls[-1]
    assert (call["url"], call["tool"], call["args"]) == (
        PRESETS[provider].url,
        tool,
        args,
    )
    assert "https://a.example" in out


async def test_keyless_unless_a_key_is_saved(server: SimpleNamespace) -> None:
    for _ in range(2):
        server.answers.append(MCPToolResult(content=[_text("ok")]))
    await _search(SearchConfig(provider="tavily"), query="q")
    assert server.calls[-1]["headers"] == {"X-Tavily-Access-Mode": "keyless"}
    await _search(SearchConfig(provider="tavily", keys={"tavily": "tvly-1"}), query="q")
    assert server.calls[-1]["headers"] == {"Authorization": "Bearer tvly-1"}


async def test_a_refusal_is_an_error_in_the_service_s_words(
    server: SimpleNamespace,
) -> None:
    # Tavily's spent allowance: a JSON message flagged as no error.
    spent = json.dumps({"code": "monthly_cap_reached", "message": "Limit reached."})
    server.answers.append(MCPToolResult(content=[_text(spent)]))
    with pytest.raises(ToolError, match="Tavily search failed: Limit reached."):
        await _search(SearchConfig(provider="tavily"), query="q")
    server.answers.append(MCPToolResult(content=[_text("bad args")], is_error=True))
    with pytest.raises(ToolError, match="Exa search failed: bad args"):
        await _search(SearchConfig(provider="exa"), query="q")


async def test_an_unreachable_service_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def session(self: MCPServerStreamableHTTP):  # type: ignore[no-untyped-def]
        raise ConnectionError("refused")
        yield  # pragma: no cover

    monkeypatch.setattr(MCPServerStreamableHTTP, "session", session)
    with pytest.raises(ToolError, match="Parallel search could not be reached"):
        await _search(SearchConfig(), query="q")


def test_off_is_no_tool() -> None:
    assert search_tool(SearchConfig(provider="off")) is None


def test_json_results_become_numbered_hits_within_bounds() -> None:
    rows = [
        {"title": f"T{i}", "url": f"https://x/{i}", "excerpts": ["e" * 5_000]}
        for i in range(8)
    ]
    out = compact_results(json.dumps({"search_id": "s", "results": rows}), 3)
    hits = out.split("\n\n")
    assert len(hits) == 3
    assert hits[0].startswith("1. T0\nhttps://x/0\neee")
    assert len(hits[0]) < 1_600  # one hit's excerpt is clipped
    # Tavily's shape: ``content`` for the excerpt.
    tavily = {"results": [{"title": "", "url": "https://t", "content": "c"}]}
    assert compact_results(json.dumps(tavily), 5) == "1. (untitled)\nhttps://t\nc"
    assert compact_results(json.dumps({"results": []}), 5) == "No results."


def test_other_answers_pass_through_clipped() -> None:
    assert compact_results("Title: A\nURL: https://a", 5) == "Title: A\nURL: https://a"
    assert len(compact_results("x" * 50_000, 5)) == 12_000
