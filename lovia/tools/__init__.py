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
    apply_tool_policies,
    default_result_renderer,
    render_tool_result,
    run_tool,
    truncate_tool_output,
    tool,
)
from .http import http_fetch
from .human import HumanChannel, HumanQuestion, ask_human
from .recall import make_recall_tool
from .search import (
    DuckDuckGoSearch,
    SearchResult,
    WebSearch,
    duckduckgo_search,
    web_search,
)
from .time import current_date, now, sleep

__all__ = [
    "ApprovalPredicate",
    "DuckDuckGoSearch",
    "HumanChannel",
    "HumanQuestion",
    "SearchResult",
    "Tool",
    "ToolInvoker",
    "ToolPolicy",
    "ToolResultRenderer",
    "WebSearch",
    "apply_tool_policies",
    "ask_human",
    "current_date",
    "default_result_renderer",
    "duckduckgo_search",
    "http_fetch",
    "make_recall_tool",
    "now",
    "render_tool_result",
    "truncate_tool_output",
    "run_tool",
    "sleep",
    "tool",
    "web_search",
]
