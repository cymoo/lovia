"""Tools: the :class:`Tool` type, the ``@tool`` decorator, and built-ins.

Infrastructure (defining and running tools) lives in :mod:`lovia.tools.base`;
the built-in tools live in their own modules and are re-exported here. The
workspace file/shell tools read the active session from
``RunContext.workspace`` — configure ``Agent(workspace=Workspace.local(...))``
and they are added to the agent automatically.
"""

from __future__ import annotations

from .base import (
    ApprovalPredicate,
    Tool,
    ToolInvoker,
    ToolPolicy,
    ToolResultRenderer,
    result_to_text,
    tool,
)
from .fence import FENCE_NOTE, fence
from .human import HumanChannel, HumanQuestion, QuestionOption, ask_human
from .page import (
    HttpReader,
    Page,
    PageImage,
    PageReader,
    html_to_markdown,
    web_fetch,
)
from .recall import recall_tool_result
from .search import (
    DuckDuckGoSearch,
    SearchResult,
    TavilySearch,
    WebSearch,
    duckduckgo_search,
    tavily_search,
    web_search,
)
from .time import current_date, now

__all__ = [
    "ApprovalPredicate",
    "DuckDuckGoSearch",
    "FENCE_NOTE",
    "HttpReader",
    "HumanChannel",
    "HumanQuestion",
    "Page",
    "PageImage",
    "PageReader",
    "QuestionOption",
    "SearchResult",
    "TavilySearch",
    "Tool",
    "ToolInvoker",
    "ToolPolicy",
    "ToolResultRenderer",
    "WebSearch",
    "ask_human",
    "current_date",
    "duckduckgo_search",
    "fence",
    "html_to_markdown",
    "now",
    "recall_tool_result",
    "result_to_text",
    "tavily_search",
    "tool",
    "web_fetch",
    "web_search",
]
