"""Pluggable web-search tool.

The :class:`WebSearch` :class:`typing.Protocol` is the extension point —
implement it for whatever backend you like. A default
:class:`DuckDuckGoSearch` is provided behind the optional
``lovia[tools]`` extra so users can get started without an API key::

    from lovia.builtins.search import DuckDuckGoSearch, web_search
    search = web_search(DuckDuckGoSearch())   # or your own implementation
    agent = Agent(name="x", tools=[search])

The factory ``web_search(impl)`` returns a single :class:`Tool` whose name
defaults to ``web_search``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from ..exceptions import UserError
from ..tools import Tool, tool


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class WebSearch(Protocol):
    """Minimum surface for a search backend.

    Implementations must be safe to call concurrently.
    """

    async def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        ...


class DuckDuckGoSearch:
    """Default backend using ``duckduckgo-search`` (install with ``lovia[tools]``)."""

    async def search(
        self, query: str, *, max_results: int = 5
    ) -> list[SearchResult]:
        try:
            from duckduckgo_search import DDGS  # type: ignore[import-not-found]
        except ImportError as exc:
            raise UserError(
                "DuckDuckGoSearch requires the 'duckduckgo-search' package.",
                hint="Install with: pip install 'lovia[tools]'",
            ) from exc

        def _go() -> list[dict[str, Any]]:
            with DDGS() as ddgs:
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


def web_search(impl: WebSearch | None = None, *, name: str = "web_search") -> Tool:
    """Build a ``web_search`` :class:`Tool` backed by ``impl``.

    When ``impl`` is ``None``, falls back to :class:`DuckDuckGoSearch`.
    """
    backend = impl or DuckDuckGoSearch()

    @tool(name=name)
    async def _search(
        query: Annotated[str, "Search query."],
        max_results: Annotated[int, "Max results (1-20)."] = 5,
    ) -> list[dict[str, str]]:
        """Search the web and return a list of ``{title, url, snippet}``."""
        n = max(1, min(int(max_results), 20))
        rows = await backend.search(query, max_results=n)
        return [
            {"title": r.title, "url": r.url, "snippet": r.snippet} for r in rows
        ]

    return _search
