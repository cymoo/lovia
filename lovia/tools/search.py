"""Pluggable web-search tool.

The :class:`WebSearch` :class:`typing.Protocol` is the extension point —
implement it over whatever search API you use, and :func:`web_search` turns
it into a tool with one stable shape (``query``, ``max_results``, an optional
``time_range`` recency filter of ``'d'``/``'w'``/``'m'``/``'y'``)::

    class MySearch:
        async def search(self, query, *, max_results=5, time_range=None):
            ...  # call the API, return list[SearchResult]

    agent = Agent(name="x", tools=[web_search(MySearch())])

lovia bundles no backend: search services are commercial APIs that come and
go, and a dozen lines over httpx reach any of them (docs/en/built-in-tools.md
has one). A hosted search MCP server works too, through
:mod:`lovia.plugins.mcp`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from .base import Tool, result_to_text, tool
from .fence import FENCE_NOTE, fence

__all__ = ["SearchResult", "WebSearch", "web_search"]


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


def _render_search_results(result: Any, ctx: Any) -> str:
    """The text both the model and the web UI see for a ``web_search`` result.

    A readable block per hit (title / url / snippet) instead of raw JSON; the
    bare URL on its own line is what the web UI turns into a clickable link.
    Titles and snippets are the open web's words, so the hits come back
    fenced. Only the success shape (a list of hits) is ours to format — the runner
    never routes error strings through renderers, so anything else can only
    arrive from a direct caller and falls back to the default rendering.
    """
    if not isinstance(result, list):
        return result_to_text(result)
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
    return fence("\n\n".join(blocks))


def web_search(impl: WebSearch, *, name: str = "web_search") -> Tool:
    """Build a ``web_search`` :class:`Tool` backed by ``impl``."""

    # Search crosses the open internet: a dropped connection is a normal
    # event, and retrying here costs a moment where letting it through costs
    # the model a whole turn to notice and re-ask.
    @tool(
        name=name,
        result_renderer=_render_search_results,
        retries=2,
        description=(
            "Search the web and return a list of {title, url, snippet}.\n"
            f"{FENCE_NOTE}"
        ),
    )
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
