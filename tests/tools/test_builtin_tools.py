"""Tests for the bundled lovia.tools.* submodules."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pytest

from lovia.exceptions import ToolError, UserError
from lovia.run_context import RunContext
from lovia.tools._http import decode_body
from lovia.tools.base import run_tool
from lovia.tools.human import HumanChannel, ask_human
from lovia.tools.search import SearchResult, web_search
from lovia.tools.time import current_date, now


def _ctx() -> RunContext:
    return RunContext(deps=None, entries=[], agent=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- http


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    target: str,
) -> None:
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(target, factory)


def test_decode_body_prefers_header_then_meta_then_utf8() -> None:
    gbk = "<meta charset=gbk><p>中文</p>".encode("gbk")
    # No header charset and no sniffing: utf-8 mangles a GBK body.
    assert "中文" not in decode_body(gbk, None)
    # Sniffing the document's own declaration recovers it...
    assert "中文" in decode_body(gbk, None, sniff_meta=True)
    # ...but an explicit header still wins, and an unusable one falls through
    # to the sniffed value rather than raising LookupError.
    assert "中文" in decode_body(gbk, "gbk")
    assert "中文" in decode_body(gbk, "totally-bogus", sniff_meta=True)


# ---------------------------------------------------------------- time


@pytest.mark.asyncio
async def test_now_defaults_to_local_iso() -> None:
    result = await now.invoke({}, _ctx())
    parsed = datetime.fromisoformat(result)  # valid ISO-8601...
    assert parsed.tzinfo is not None  # ...carrying an explicit UTC offset
    # Default is the server's local zone, not UTC.
    assert parsed.utcoffset() == datetime.now().astimezone().utcoffset()


@pytest.mark.asyncio
async def test_now_accepts_explicit_tz() -> None:
    assert (await now.invoke({"tz": "UTC"}, _ctx())).endswith("+00:00")


@pytest.mark.asyncio
async def test_now_unknown_timezone_raises_tool_error() -> None:
    # A bad / unknown zone (incl. Windows-without-tzdata) gives a clear error,
    # not a raw ZoneInfoNotFoundError traceback.
    with pytest.raises(ToolError, match="Unknown timezone"):
        await now.invoke({"tz": "Not/AZone"}, _ctx())


# ---------------------------------------------------------------- current_date


def test_current_date_states_today_with_weekday() -> None:
    # The fragment is what the model sees before it acts: today's ISO date + day.
    today = datetime.now().astimezone()
    text = current_date()(_ctx())
    assert text.startswith("Today's date is ")
    assert today.strftime("%Y-%m-%d") in text
    assert today.strftime("%A") in text


def test_current_date_honors_explicit_tz() -> None:
    text = current_date(tz="UTC")(_ctx())
    assert datetime.now(timezone.utc).strftime("%Y-%m-%d") in text


def test_current_date_unknown_timezone_raises_at_construction() -> None:
    # A bad zone fails when the fragment is built, not on every render.
    with pytest.raises(UserError, match="Unknown timezone"):
        current_date(tz="Not/AZone")


# ---------------------------------------------------------------- search


@pytest.mark.asyncio
async def test_web_search_with_custom_backend() -> None:
    class Stub:
        async def search(self, query: str, *, max_results: int = 5, time_range=None):  # type: ignore[no-untyped-def]
            return [SearchResult(title="t", url="https://x", snippet="s")]

    s = web_search(Stub())
    out = await s.invoke({"query": "x"}, _ctx())
    assert out == [{"title": "t", "url": "https://x", "snippet": "s"}]


@pytest.mark.asyncio
async def test_web_search_forwards_time_range() -> None:
    seen: dict[str, Any] = {}

    class Recorder:
        async def search(self, query: str, *, max_results: int = 5, time_range=None):  # type: ignore[no-untyped-def]
            seen["time_range"] = time_range
            return [SearchResult(title="t", url="https://x", snippet="s")]

    s = web_search(Recorder())
    await s.invoke({"query": "x", "time_range": "m"}, _ctx())
    assert seen["time_range"] == "m"
    # Omitting it means no recency filter — the backend receives None.
    await s.invoke({"query": "x"}, _ctx())
    assert seen["time_range"] is None


def test_web_search_time_range_is_an_enum_in_the_schema() -> None:
    # The model sees an explicit d/w/m/y choice, so invalid values can't arrive.
    class Stub:
        async def search(self, query: str, *, max_results: int = 5, time_range=None):  # type: ignore[no-untyped-def]
            return []

    prop = web_search(Stub()).parameters["properties"]["time_range"]
    enum = prop.get("enum") or next(
        b["enum"] for b in prop.get("anyOf", []) if "enum" in b
    )
    assert enum == ["d", "w", "m", "y"]


def test_web_search_result_renderer() -> None:
    # The renderer is what the model AND the web UI see — readable text with the
    # url on its own line (which the UI linkifies), not raw JSON.
    from lovia.tools.search import _render_search_results

    hits = [
        {"title": "First", "url": "https://a.example", "snippet": "alpha"},
        {"title": "Second", "url": "https://b.example", "snippet": "beta"},
    ]
    out = _render_search_results(hits, None)
    assert "First" in out and "https://a.example" in out and "alpha" in out
    assert "Second" in out
    assert "[{" not in out and '"title"' not in out  # not raw JSON
    assert _render_search_results([], None) == "No results."
    # Non-list shapes can only arrive from a direct caller (the runner never
    # routes error strings through renderers); they fall back to the default
    # rendering rather than being swallowed as "No results.".
    assert _render_search_results("plain string", None) == "plain string"


@pytest.mark.asyncio
async def test_web_search_clamps_max_results() -> None:
    seen: dict[str, Any] = {}

    class Recorder:
        async def search(self, query: str, *, max_results: int = 5, time_range=None):  # type: ignore[no-untyped-def]
            seen["max_results"] = max_results
            return []

    s = web_search(Recorder())
    await s.invoke({"query": "x", "max_results": 100}, _ctx())
    assert seen["max_results"] == 20
    await s.invoke({"query": "x", "max_results": 0}, _ctx())
    assert seen["max_results"] == 1


@pytest.mark.asyncio
async def test_web_search_retries_a_dropped_connection() -> None:
    """One blip must not cost a model turn: the tool retries in place."""

    attempts = {"n": 0}

    class Flaky:
        async def search(self, query: str, *, max_results: int = 5, time_range=None):  # type: ignore[no-untyped-def]
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionResetError()  # message-less, like the real ones
            return [SearchResult(title="t", url="https://x", snippet="s")]

    out = await run_tool(web_search(Flaky()), {"query": "x"}, _ctx())
    assert out == [{"title": "t", "url": "https://x", "snippet": "s"}]
    assert attempts["n"] == 3


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
    # An answered question is fully forgotten.
    assert channel.pending == [] and channel._futures == {}


@pytest.mark.asyncio
async def test_ask_human_cancel_raises_tool_error() -> None:
    channel = HumanChannel()
    tool_ = ask_human(channel)

    task = asyncio.create_task(tool_.invoke({"question": "hi?"}, _ctx()))
    await asyncio.sleep(0.01)
    channel.cancel(channel.pending[0].id, "operator went home")
    with pytest.raises(ToolError, match="operator went home"):
        await task
    assert channel.pending == [] and channel._futures == {}


@pytest.mark.asyncio
async def test_ask_human_external_cancellation_cleans_up_channel() -> None:
    # A tool timeout / run cancellation cancels the awaiting task from the
    # outside; the channel must not keep a ghost question nobody can answer.
    channel = HumanChannel()
    tool_ = ask_human(channel)

    task = asyncio.create_task(tool_.invoke({"question": "hi?"}, _ctx()))
    await asyncio.sleep(0.01)
    assert len(channel.pending) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert channel.pending == [] and channel._futures == {}


@pytest.mark.asyncio
async def test_ask_human_close_cancels_all_pending() -> None:
    channel = HumanChannel()
    tool_ = ask_human(channel)

    t1 = asyncio.create_task(tool_.invoke({"question": "one?"}, _ctx()))
    t2 = asyncio.create_task(tool_.invoke({"question": "two?"}, _ctx()))
    await asyncio.sleep(0.01)
    assert len(channel.pending) == 2
    channel.close("shutting down")
    for t in (t1, t2):
        with pytest.raises(ToolError, match="shutting down"):
            await t
    assert channel.pending == [] and channel._futures == {}


@pytest.mark.asyncio
async def test_ask_human_concurrent_questions_resolve_independently() -> None:
    channel = HumanChannel()
    tool_ = ask_human(channel)

    t1 = asyncio.create_task(tool_.invoke({"question": "first?"}, _ctx()))
    t2 = asyncio.create_task(tool_.invoke({"question": "second?"}, _ctx()))
    await asyncio.sleep(0.01)
    by_text = {q.question: q.id for q in channel.pending}
    channel.answer(by_text["second?"], "B")  # out of order on purpose
    channel.answer(by_text["first?"], "A")
    assert await t1 == "A"
    assert await t2 == "B"
