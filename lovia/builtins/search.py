"""Compatibility wrapper for :mod:`lovia.tools.search`."""

from __future__ import annotations

from ..tools.search import (
    DuckDuckGoSearch,
    SearchResult,
    WebSearch,
    duckduckgo_search_tool,
    web_search,
)

__all__ = [
    "DuckDuckGoSearch",
    "SearchResult",
    "WebSearch",
    "duckduckgo_search_tool",
    "web_search",
]
