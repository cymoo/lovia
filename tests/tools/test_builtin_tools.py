"""Tests for the bundled lovia.tools.* submodules."""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Callable

import httpx
import pytest

from lovia.exceptions import ToolError, UserError
from lovia.run_context import RunContext
from lovia.tools.http import html_to_text, http_fetch
from lovia.tools.human import HumanChannel, ask_human
from lovia.tools.search import SearchResult, duckduckgo_search_tool, web_search
from lovia.tools.time import now, sleep


def _ctx() -> RunContext:
    return RunContext(context=None, entries=[], agent=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- http


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("lovia.tools.http.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_http_fetch_compacts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, json={"a": [1, 2], "b": "x"}),
    )
    out = await http_fetch.invoke({"url": "https://api.example.com/data"}, _ctx())
    assert out.startswith("HTTP 200 · application/json")
    assert '{"a":[1,2],"b":"x"}' in out


@pytest.mark.asyncio
async def test_http_fetch_extracts_html_text(monkeypatch: pytest.MonkeyPatch) -> None:
    html = (
        "<html><head><title>T</title><script>evil()</script>"
        "<style>p{}</style></head>"
        "<body><h1>Heading</h1><p>Hello <b>world</b></p></body></html>"
    )
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, text=html, headers={"content-type": "text/html; charset=utf-8"}
        ),
    )
    out = await http_fetch.invoke({"url": "https://example.com"}, _ctx())
    assert "Heading" in out and "Hello world" in out
    assert "evil()" not in out and "<p>" not in out


@pytest.mark.asyncio
async def test_http_fetch_binary_returns_metadata_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=b"\x00\x01\x02\x03",
            headers={"content-type": "application/octet-stream"},
        ),
    )
    out = await http_fetch.invoke({"url": "https://example.com/blob"}, _ctx())
    assert "binary content not shown" in out
    assert "application/octet-stream" in out


@pytest.mark.asyncio
async def test_http_fetch_truncates_large_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, text="x" * 5_000, headers={"content-type": "text/plain"}
        ),
    )
    out = await http_fetch.invoke(
        {"url": "https://example.com/big", "max_chars": 200}, _ctx()
    )
    assert "truncated" in out
    assert len(out) < 1_000


@pytest.mark.asyncio
async def test_http_fetch_rejects_non_http_schemes() -> None:
    with pytest.raises(ToolError, match="scheme"):
        await http_fetch.invoke({"url": "file:///etc/passwd"}, _ctx())


def test_html_to_text_handles_malformed_markup() -> None:
    assert html_to_text("<p>ok<div") == "ok"
    assert html_to_text("plain text") == "plain text"


# ---------------------------------------------------------------- time


@pytest.mark.asyncio
async def test_now_returns_iso() -> None:
    result = await now.invoke({}, _ctx())
    assert "T" in result and (
        result.endswith("+00:00") or "+" in result or "-" in result[10:]
    )


@pytest.mark.asyncio
async def test_sleep_is_capped() -> None:
    out = await sleep.invoke({"seconds": 0.01}, _ctx())
    assert "slept" in out


# ---------------------------------------------------------------- search


@pytest.mark.asyncio
async def test_web_search_with_custom_backend() -> None:
    class Stub:
        async def search(self, query: str, *, max_results: int = 5):  # type: ignore[no-untyped-def]
            return [SearchResult(title="t", url="https://x", snippet="s")]

    s = web_search(Stub())
    out = await s.invoke({"query": "x"}, _ctx())
    assert out == [{"title": "t", "url": "https://x", "snippet": "s"}]


def test_duckduckgo_friendly_error_without_dep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force both ddgs and duckduckgo_search imports to fail.
    for mod in ("ddgs", "duckduckgo_search"):
        if mod in sys.modules:
            monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setattr(
        "builtins.__import__",
        lambda name, *a, **k: (
            (_ for _ in ()).throw(ImportError(name))
            if name in ("ddgs", "duckduckgo_search")
            else __import__(name, *a, **k)
        ),
    )
    with pytest.raises(UserError) as exc_info:
        duckduckgo_search_tool()
    assert "lovia[ddg]" in str(exc_info.value)


# ---------------------------------------------------------------- human


@pytest.mark.asyncio
async def test_ask_human_resolves_via_channel() -> None:
    channel = HumanChannel()
    tool_ = ask_human(channel)

    async def answerer() -> None:
        await asyncio.sleep(0.01)
        pending = channel.pending
        assert pending
        channel.answer(pending[0].id, "42")

    t = asyncio.create_task(answerer())
    result = await tool_.invoke({"question": "what is the answer?"}, _ctx())
    await t
    assert result == "42"
