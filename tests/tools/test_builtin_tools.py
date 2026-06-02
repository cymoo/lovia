"""Tests for the bundled lovia.tools.* submodules."""

from __future__ import annotations

import asyncio
import sys

import pytest

from lovia.tools.http import http_fetch
from lovia.tools.human import HumanChannel, ask_human
from lovia.tools.search import SearchResult, duckduckgo_search_tool, web_search
from lovia.tools.time import now, sleep
from lovia.tools.todo import TodoList, todo_tools
from lovia.exceptions import UserError
from lovia.run_context import RunContext


def _ctx() -> RunContext:
    return RunContext(context=None, entries=[], agent=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- http


def test_http_tool_metadata() -> None:
    assert http_fetch.name == "http_fetch"
    assert "url" in http_fetch.parameters["properties"]


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


# ---------------------------------------------------------------- todo


@pytest.mark.asyncio
async def test_todo_lifecycle() -> None:
    todos = TodoList()
    tools = {t.name: t for t in todo_tools(todos)}
    tid = await tools["add_todo"].invoke({"title": "do it"}, _ctx())
    await tools["update_todo"].invoke({"id": tid, "status": "done"}, _ctx())
    rendered = await tools["list_todos"].invoke({}, _ctx())
    assert "[x]" in rendered and "do it" in rendered


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
    assert "lovia[tools]" in str(exc_info.value)


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
