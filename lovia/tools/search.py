"""Pluggable web-search tool.

The :class:`WebSearch` :class:`typing.Protocol` is the extension point —
implement it for whatever backend you like. A convenience
:func:`duckduckgo_search` factory is provided behind the optional
``lovia[ddg]`` extra so users can get started without an API key::

    from lovia.tools.search import duckduckgo_search, web_search

    search = duckduckgo_search()         # requires lovia[ddg]
    custom = web_search(MySearchBackend())    # or your own implementation
    agent = Agent(name="x", tools=[search])

The factory ``web_search(impl)`` returns a single :class:`Tool` whose name
defaults to ``web_search``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from ..exceptions import UserError
from .base import Tool, default_result_renderer, tool

__all__ = [
    "DuckDuckGoSearch",
    "SearchResult",
    "WebSearch",
    "duckduckgo_search",
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
        self, query: str, *, max_results: int = 5
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

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:

        def _go() -> list[dict[str, Any]]:
            with self._ddgs_cls() as ddgs:
                return list(ddgs.text(query, max_results=max_results))

        rows = await asyncio.to_thread(_go)
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("href") or r.get("url", ""),
                snippet=r.get("body", ""),
            )
            for r in rows
        ]


def _render_search_results(result: Any, ctx: Any) -> str:
    """The text both the model and the web UI see for a ``web_search`` result.

    A readable block per hit (title / url / snippet) instead of raw JSON; the
    bare URL on its own line is what the web UI turns into a clickable link.
    Only the success shape (a list of hits) is ours to format — anything else,
    notably the runner's ``"Tool error: …"`` string from a raised exception,
    passes through unchanged so real failures aren't swallowed as "No results.".
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
    ) -> list[dict[str, str]]:
        """Search the web and return a list of ``{title, url, snippet}``."""
        n = max(1, min(int(max_results), 20))
        rows = await impl.search(query, max_results=n)
        return [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in rows]

    return _search


def duckduckgo_search(*, name: str = "web_search") -> Tool:
    """Build a ``web_search`` tool using the optional DuckDuckGo backend."""
    return web_search(DuckDuckGoSearch(), name=name)
