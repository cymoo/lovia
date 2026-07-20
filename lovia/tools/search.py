"""Pluggable web-search tool.

The :class:`WebSearch` :class:`typing.Protocol` is the extension point —
implement it for whatever backend you like. Two convenience factories are
bundled: :func:`duckduckgo_search` (keyless, behind the optional
``lovia[ddg]`` extra) and :func:`tavily_search` (no extra install — httpx is
a core dependency — but needs ``TAVILY_API_KEY``)::

    from lovia.tools.search import duckduckgo_search, tavily_search, web_search

    search = duckduckgo_search()         # requires lovia[ddg]
    keyed = tavily_search()              # requires TAVILY_API_KEY
    custom = web_search(MySearchBackend())    # or your own implementation
    agent = Agent(name="x", tools=[search])

The factory ``web_search(impl)`` returns a single :class:`Tool` whose name
defaults to ``web_search``. The tool also exposes an optional ``time_range``
recency filter (``'d'``/``'w'``/``'m'``/``'y'``) that backends receive as a
keyword argument.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

import httpx

from ..exceptions import ToolError, UserError
from ..http_config import resolve_verify
from .base import Tool, default_result_renderer, tool

__all__ = [
    "DuckDuckGoSearch",
    "SearchResult",
    "TavilySearch",
    "WebSearch",
    "duckduckgo_search",
    "tavily_search",
    "web_search",
]


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class WebSearch(Protocol):
    """Minimum surface for a search backend.

    Implementations must be safe to call concurrently.
    """

    async def search(
        self, query: str, *, max_results: int = 5, time_range: str | None = None
    ) -> list[SearchResult]: ...


class DuckDuckGoSearch:
    """Default backend using ``ddgs`` (install with ``lovia[ddg]``)."""

    def __init__(self) -> None:
        ddgs_cls: Any
        try:
            try:
                from ddgs import DDGS as ddgs_impl

                ddgs_cls = ddgs_impl
            except ImportError:
                from duckduckgo_search import (  # type: ignore[import-not-found]
                    DDGS as duckduckgo_impl,
                )

                ddgs_cls = duckduckgo_impl
        except ImportError as exc:
            raise UserError(
                "DuckDuckGoSearch requires the 'ddgs' package.",
                hint="Install with: pip install 'lovia[ddg]'",
            ) from exc
        self._ddgs_cls = ddgs_cls

    async def search(
        self, query: str, *, max_results: int = 5, time_range: str | None = None
    ) -> list[SearchResult]:

        def _go() -> list[dict[str, Any]]:
            with self._ddgs_cls() as ddgs:
                # ``timelimit`` is DDG's recency filter ('d'/'w'/'m'/'y'); None = no limit.
                return list(
                    ddgs.text(query, max_results=max_results, timelimit=time_range)
                )

        rows = await asyncio.to_thread(_go)
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("href") or r.get("url", ""),
                snippet=r.get("body", ""),
            )
            for r in rows
        ]


_TAVILY_ENDPOINT = "https://api.tavily.com/search"


class TavilySearch:
    """Backend using the Tavily Search API (requires an API key)."""

    def __init__(self, *, api_key: str | None = None, timeout: float = 30.0) -> None:
        key = api_key or os.environ.get("TAVILY_API_KEY")
        if not key:
            raise UserError(
                "TavilySearch requires an API key.",
                hint="Set TAVILY_API_KEY or pass api_key=...",
            )
        self._api_key = key
        self._timeout = timeout

    async def search(
        self, query: str, *, max_results: int = 5, time_range: str | None = None
    ) -> list[SearchResult]:
        payload: dict[str, Any] = {"query": query, "max_results": max_results}
        if time_range is not None:
            # Tavily accepts 'd'/'w'/'m'/'y' directly (alongside 'day'/'week'/...).
            payload["time_range"] = time_range
        async with httpx.AsyncClient(
            timeout=self._timeout, verify=resolve_verify()
        ) as client:
            resp = await client.post(
                _TAVILY_ENDPOINT,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        if resp.status_code != 200:
            raise ToolError(
                f"Tavily search failed (HTTP {resp.status_code}): "
                f"{_tavily_error(resp)}",
                hint=(
                    "Check TAVILY_API_KEY."
                    if resp.status_code == 401
                    else "Tavily plan/usage limit reached."
                    if resp.status_code in (432, 433)
                    else None
                ),
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise ToolError(
                "Tavily search failed: non-JSON response from api.tavily.com."
            ) from exc
        rows = body.get("results") if isinstance(body, dict) else None
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
            )
            for r in rows or []
        ]


def _tavily_error(resp: httpx.Response) -> str:
    # Error bodies look like {"detail": {"error": "..."}}; fall back to raw text.
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("error"))
    return str(detail) if detail is not None else resp.text[:200]


def _render_search_results(result: Any, ctx: Any) -> str:
    """The text both the model and the web UI see for a ``web_search`` result.

    A readable block per hit (title / url / snippet) instead of raw JSON; the
    bare URL on its own line is what the web UI turns into a clickable link.
    Only the success shape (a list of hits) is ours to format — the runner
    never routes error strings through renderers, so anything else can only
    arrive from a direct caller and falls back to the default rendering.
    """
    if not isinstance(result, list):
        return result if isinstance(result, str) else default_result_renderer(result)
    if not result:
        return "No results."
    blocks: list[str] = []
    for i, row in enumerate(result, 1):
        title = (row.get("title") or "").strip() or "(untitled)"
        url = (row.get("url") or "").strip()
        snippet = (row.get("snippet") or "").strip()
        block = f"{i}. {title}"
        if url:
            block += f"\n{url}"
        if snippet:
            block += f"\n{snippet}"
        blocks.append(block)
    return "\n\n".join(blocks)


def web_search(impl: WebSearch, *, name: str = "web_search") -> Tool:
    """Build a ``web_search`` :class:`Tool` backed by ``impl``.

    Pass an explicit backend so optional dependencies fail at construction
    time instead of during a later agent run. Use
    :func:`duckduckgo_search` for the bundled DuckDuckGo backend.
    """

    @tool(name=name, result_renderer=_render_search_results)
    async def _search(
        query: Annotated[str, "Search query."],
        max_results: Annotated[int, "Max results (1-20)."] = 5,
        time_range: Annotated[
            Literal["d", "w", "m", "y"] | None,
            "Optional recency filter: 'd'=past day, 'w'=week, 'm'=month, 'y'=year. "
            "Omit for no time limit.",
        ] = None,
    ) -> list[dict[str, str]]:
        """Search the web and return a list of ``{title, url, snippet}``."""
        n = max(1, min(int(max_results), 20))
        rows = await impl.search(query, max_results=n, time_range=time_range)
        return [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in rows]

    return _search


def duckduckgo_search(*, name: str = "web_search") -> Tool:
    """Build a ``web_search`` tool using the optional DuckDuckGo backend."""
    return web_search(DuckDuckGoSearch(), name=name)


def tavily_search(*, api_key: str | None = None, name: str = "web_search") -> Tool:
    """Build a ``web_search`` tool using the Tavily backend (needs an API key)."""
    return web_search(TavilySearch(api_key=api_key), name=name)
