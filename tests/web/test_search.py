"""Web search over a hosted search MCP server (lovia/web/search.py)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from lovia.exceptions import ToolError  # noqa: E402
from lovia.plugins.mcp import MCPServerStreamableHTTP, MCPToolResult  # noqa: E402
from lovia.web.config.schema import SearchConfig  # noqa: E402
from lovia.web.search import (  # noqa: E402
    PRESETS,
    compact_results,
    search_tool,
    server_config,
)


# What a fake server lists: one tool, as the search opens it.
name_listed = SimpleNamespace(name="search__tool")


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

        yield SimpleNamespace(call=call, tools=lambda: [name_listed])

    monkeypatch.setattr(MCPServerStreamableHTTP, "session", session)
    return fake


async def _search(search: SearchConfig, **args: Any) -> Any:
    tool = search_tool(search)
    assert tool is not None
    return await tool.invoke(args, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("provider", "tool", "args"),
    [
        (
            "parallel",
            "web_search",
            {
                "objective": "q (only pages published in the past week)",
                "search_queries": ["q"],
            },
        ),
        (
            "tavily",
            "tavily_search",
            {"query": "q", "max_results": 3, "time_range": "week"},
        ),
        (
            "exa",
            "web_search_exa",
            {
                "query": "q",
                "objective": "q (only pages published in the past week)",
                "numResults": 3,
            },
        ),
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
    with pytest.raises(ToolError, match="Tavily search failed:") as err:
        await _search(SearchConfig(provider="tavily"), query="q")
    # The service's words, fenced like any answer of its.
    assert "BEGIN EXTERNAL CONTENT" in str(err.value)
    assert "Limit reached." in str(err.value)
    server.answers.append(MCPToolResult(content=[_text("bad args")], is_error=True))
    with pytest.raises(ToolError, match="Exa search failed:"):
        await _search(SearchConfig(provider="exa"), query="q")
    # A refusal is answered once, not replayed.
    assert len(server.calls) == 2


async def test_only_a_connection_that_never_opened_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opens: list[str] = []

    def fake_server(fail_open: int, fail_call: bool) -> None:
        @asynccontextmanager
        async def session(self: MCPServerStreamableHTTP):  # type: ignore[no-untyped-def]
            opens.append("open")
            if len(opens) <= fail_open:
                raise ConnectionResetError()

            async def call(name: str, args: dict[str, Any]) -> MCPToolResult:
                if fail_call:
                    raise ConnectionResetError()  # the reply lost, the search run
                return MCPToolResult(content=[_text("ok")])

            yield SimpleNamespace(call=call, tools=lambda: [name_listed])

        monkeypatch.setattr(MCPServerStreamableHTTP, "session", session)

    one = SearchConfig(provider="parallel")
    fake_server(fail_open=1, fail_call=False)
    assert "ok" in await _search(one, query="q")
    assert len(opens) == 2
    opens.clear()
    fake_server(fail_open=0, fail_call=True)
    with pytest.raises(ToolError, match="Parallel search did not complete"):
        await _search(one, query="q")
    assert len(opens) == 1  # never searched twice


async def test_an_unreachable_service_says_so_without_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def session(self: MCPServerStreamableHTTP):  # type: ignore[no-untyped-def]
        # httpx quotes a header it refuses, key and all.
        raise ValueError("Invalid header value b'Bearer par-secret '")
        yield  # pragma: no cover

    monkeypatch.setattr(MCPServerStreamableHTTP, "session", session)
    with pytest.raises(ToolError, match="Parallel search could not be reached") as err:
        await _search(
            SearchConfig(provider="parallel", keys={"parallel": "par-secret"}),
            query="q",
        )
    assert "par-secret" not in str(err.value) and "***" in str(err.value)


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
    assert hits[0].startswith("[1] T0\nhttps://x/0\neee")
    assert len(hits[0]) < 1_600  # one hit's excerpt is clipped
    # Tavily's shape: ``content`` for the excerpt.
    tavily = {"results": [{"title": "", "url": "https://t", "content": "c"}]}
    assert compact_results(json.dumps(tavily), 5) == "[1] (untitled)\nhttps://t\nc"
    assert compact_results(json.dumps({"results": []}), 5) == "No results."


def test_an_excerpt_loses_its_page_s_front_matter() -> None:
    excerpt = '---\ntitle: "T"\nurl: x.example/a\n---\n# T\n\n1. Home\n---\nend'
    row = {"title": "T", "url": "https://x.example/a", "excerpts": [excerpt]}
    out = compact_results(json.dumps({"results": [row]}), 5)
    # The rule further down is the page's own.
    assert out == "[1] T\nhttps://x.example/a\n# T\n\n1. Home\n---\nend"


def test_dated_hits_before_the_range_are_left_out() -> None:
    rows = [
        {"title": "old", "url": "https://o", "publish_date": "2026-10-01"},
        {"title": "new", "url": "https://n", "publish_date": "2026-11-26T08:00:00Z"},
        {"title": "undated", "url": "https://u", "publish_date": None},
    ]
    text = json.dumps({"results": rows})
    out = compact_results(text, 5, since=date(2026, 11, 20))
    assert out == "[1] new\nhttps://n\n\n[2] undated\nhttps://u"
    assert len(compact_results(text, 5).split("\n\n")) == 3  # no range, all kept
    assert (
        compact_results(json.dumps({"results": rows[:1]}), 5, since=date(2026, 11, 20))
        == "No results in that time range."
    )


def test_exa_s_text_becomes_numbered_hits_too() -> None:
    hit = "Title: {t}\nURL: https://{t}\nPublished: {p}\nAuthor: N/A\nHighlights:\n{b}"
    text = "\n\n---\n\n".join(
        [
            hit.format(t="old", p="2026-10-01T00:00:00.000Z", b="x"),
            hit.format(t="new", p="2026-11-27T06:02:25.000Z", b="y" * 5_000),
            hit.format(t="undated", p="N/A", b="z\n---\nz"),
        ]
    )
    out = compact_results(text, 5, since=date(2026, 11, 20))
    new, undated = out.split("\n\n")
    assert new.startswith("[1] new\nhttps://new\nyyy") and len(new) < 1_600
    # A rule inside the highlights is theirs; the one between hits is not.
    assert undated == "[2] undated\nhttps://undated\nz\n---\nz"


def test_other_answers_pass_through_clipped() -> None:
    assert compact_results("plain words", 5) == "plain words"
    assert len(compact_results("x" * 50_000, 5)) == 12_000


async def test_auto_moves_on_only_from_a_service_it_cannot_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    blocked = {"https://mcp.parallel.ai/mcp"}

    @asynccontextmanager
    async def session(self: MCPServerStreamableHTTP):  # type: ignore[no-untyped-def]
        opened.append(self.url)
        if self.url in blocked:
            raise RuntimeError("Redirect to http://10.0.0.5/warn not followed")

        async def call(name: str, args: dict[str, Any]) -> MCPToolResult:
            if self.url == PRESETS["exa"].url and "refuse" in args["query"]:
                return MCPToolResult(content=[_text("over quota")], is_error=True)
            return MCPToolResult(content=[_text(f"answer from {name}")])

        yield SimpleNamespace(call=call, tools=lambda: [name_listed])

    monkeypatch.setattr(MCPServerStreamableHTTP, "session", session)
    tool = search_tool(SearchConfig())  # auto, the default
    assert tool is not None
    out = await tool.invoke({"query": "q"}, None)  # type: ignore[arg-type]
    assert "answer from web_search_exa" in out
    assert opened == [PRESETS["parallel"].url, PRESETS["exa"].url]
    # The one that answered goes first for a while: the blocked one costs
    # its failure once.
    opened.clear()
    await tool.invoke({"query": "q"}, None)  # type: ignore[arg-type]
    assert opened == [PRESETS["exa"].url]
    # A refusal is an answer: never asked of the next service.
    opened.clear()
    with pytest.raises(ToolError, match="Exa search failed"):
        await tool.invoke({"query": "refuse"}, None)  # type: ignore[arg-type]
    assert opened == [PRESETS["exa"].url]
    # None reachable: each says why.
    blocked.update(PRESETS[n].url for n in PRESETS)
    tool = search_tool(SearchConfig())
    assert tool is not None
    with pytest.raises(ToolError, match="No search service could be reached") as err:
        await tool.invoke({"query": "q"}, None)  # type: ignore[arg-type]
    assert all(f"- {p.label}: " in str(err.value) for p in PRESETS.values())


def test_search_s_proxy_follows_settings_unless_set() -> None:
    # Unset: as Settings → General says (through the proxy by default).
    assert server_config(SearchConfig(), "exa").trust_env is None
    direct = SearchConfig(trust_env=False)
    assert server_config(direct, "exa").trust_env is False


async def test_auto_passes_over_a_hanging_service_or_one_without_its_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from lovia.web import search as search_mod

    monkeypatch.setattr(search_mod, "OPEN_TIMEOUT", 0.05)
    clock = [1_000.0]
    monkeypatch.setattr(search_mod, "_clock", lambda: clock[0])
    opened: list[str] = []
    hang = {PRESETS["parallel"].url}
    toolless = {PRESETS["exa"].url}

    @asynccontextmanager
    async def session(self: MCPServerStreamableHTTP):  # type: ignore[no-untyped-def]
        opened.append(self.url)
        if self.url in hang:
            await asyncio.sleep(10)  # accepts, and never finishes opening

        async def call(name: str, args: dict[str, Any]) -> MCPToolResult:
            return MCPToolResult(content=[_text(f"answer from {name}")])

        listed = [] if self.url in toolless else [name_listed]
        yield SimpleNamespace(call=call, tools=lambda: listed)

    monkeypatch.setattr(MCPServerStreamableHTTP, "session", session)
    tool = search_tool(SearchConfig())
    assert tool is not None
    # Room for the two after the first to open, on top of the search's own.
    assert tool.timeout == 60 + 2 * 0.05
    out = await tool.invoke({"query": "q"}, None)  # type: ignore[arg-type]
    assert "answer from tavily_search" in out
    assert opened == [PRESETS[n].url for n in ("parallel", "exa", "tavily")]
    # Tavily answers first while remembered; answering does not renew it ...
    opened.clear()
    clock[0] += 500
    await tool.invoke({"query": "q"}, None)  # type: ignore[arg-type]
    assert opened == [PRESETS["tavily"].url]
    # ... so once its time is up, Parallel is asked again.
    hang.clear()
    opened.clear()
    clock[0] += 200
    assert "answer from web_search" in await tool.invoke({"query": "q"}, None)  # type: ignore[arg-type]
    assert opened == [PRESETS["parallel"].url]
