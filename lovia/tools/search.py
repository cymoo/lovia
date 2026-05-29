"""Pluggable web-search tool.

The :class:`WebSearch` :class:`typing.Protocol` is the extension point —
implement it for whatever backend you like. A convenience
:func:`duckduckgo_search_tool` factory is provided behind the optional
``lovia[tools]`` extra so users can get started without an API key::

    from lovia.tools.search import duckduckgo_search_tool, web_search

    search = duckduckgo_search_tool()         # requires lovia[tools]
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
from . import Tool, tool

__all__ = [
    "DuckDuckGoSearch",
    "SearchResult",
    "WebSearch",
    "duckduckgo_search_tool",
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
    """Default backend using ``duckduckgo-search`` (install with ``lovia[tools]``)."""

    def __init__(self) -> None:
        ddgs_cls: Any
        try:
            try:
                from ddgs import DDGS as ddgs_impl  # type: ignore[import-not-found]

                ddgs_cls = ddgs_impl
            except ImportError:
                from duckduckgo_search import (  # type: ignore[import-not-found]
                    DDGS as duckduckgo_impl,
                )

                ddgs_cls = duckduckgo_impl
        except ImportError as exc:
            raise UserError(
                "DuckDuckGoSearch requires the 'ddgs' package.",
                hint="Install with: pip install 'lovia[tools]'",
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


def web_search(impl: WebSearch, *, name: str = "web_search") -> Tool:
    """Build a ``web_search`` :class:`Tool` backed by ``impl``.

    Pass an explicit backend so optional dependencies fail at construction
    time instead of during a later agent run. Use
    :func:`duckduckgo_search_tool` for the bundled DuckDuckGo backend.
    """

    @tool(name=name)
    async def _search(
        query: Annotated[str, "Search query."],
        max_results: Annotated[int, "Max results (1-20)."] = 5,
    ) -> list[dict[str, str]]:
        """Search the web and return a list of ``{title, url, snippet}``."""
        n = max(1, min(int(max_results), 20))
        rows = await impl.search(query, max_results=n)
        return [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in rows]

    return _search


def duckduckgo_search_tool(*, name: str = "web_search") -> Tool:
    """Build a ``web_search`` tool using the optional DuckDuckGo backend."""
    return web_search(DuckDuckGoSearch(), name=name)
