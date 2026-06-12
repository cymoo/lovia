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
    tool,
)
from .files import (
    edit_file,
    grep_files,
    list_files,
    read_file,
    require_workspace,
    write_file,
)
from .http import http_fetch
from .human import HumanChannel, HumanQuestion, ask_human
from .recall import recall_tool_result
from .search import (
    DuckDuckGoSearch,
    SearchResult,
    WebSearch,
    duckduckgo_search_tool,
    web_search,
)
from .shell import shell
from .time import now, sleep

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
    "default_result_renderer",
    "duckduckgo_search_tool",
    "edit_file",
    "grep_files",
    "http_fetch",
    "list_files",
    "now",
    "read_file",
    "recall_tool_result",
    "render_tool_result",
    "require_workspace",
    "run_tool",
    "shell",
    "sleep",
    "tool",
    "web_search",
    "write_file",
]
