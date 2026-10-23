"""The names of lovia's built-in tools, pinned.

Renaming a built-in tool is a breaking change: the name is stored in every
transcript that called it (``ToolCallEntry.name``), and tool views, approval
rules and evals match on it. One assertion per source, built from the real
objects, so a rename anywhere fails here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lovia import Agent, InMemoryResultStore
from lovia.handoff import Handoff, build_handoff_tool
from lovia.plugins import Memory, Plugin, Skills, Subagents, Todo
from lovia.run_context import RunContext
from lovia.testing import ScriptedProvider, text
from lovia.tools import (
    HumanChannel,
    SearchResult,
    Tool,
    ask_human,
    http_request,
    make_recall_tool,
    now,
    read_page,
    sleep,
    web_search,
)
from lovia.workspace import tools as workspace_tools

_MCP_STUB = Path(__file__).parent / "plugins" / "mcp_stub_server.py"


def _names(tools: list[Tool]) -> list[str]:
    return sorted(t.name for t in tools)


async def _plugin_names(plugin: Plugin) -> list[str]:
    inst = await plugin.setup(RunContext(deps=None, entries=[], agent=Agent(name="a")))
    try:
        return _names(inst.tools)
    finally:
        await inst.aclose()


class _NoSearch:
    async def search(
        self, query: str, *, max_results: int = 5, time_range: str | None = None
    ) -> list[SearchResult]:
        return []


def test_tools_module() -> None:
    assert _names(
        [
            http_request,
            read_page,
            now,
            sleep,
            web_search(_NoSearch()),
            ask_human(HumanChannel()),
            make_recall_tool(InMemoryResultStore()),
        ]
    ) == [
        "ask_human",
        "http_request",
        "now",
        "read_page",
        "recall_tool_result",
        "sleep",
        "web_search",
    ]


def test_agent_derived_names() -> None:
    billing = Agent(name="billing")
    assert [
        build_handoff_tool(Handoff(target=billing)).name,
        billing.as_tool().name,
    ] == ["transfer_to_billing", "ask_billing"]


def test_workspace_tools() -> None:
    assert _names(
        [
            workspace_tools.read_file,
            workspace_tools.view_image,
            workspace_tools.write_file,
            workspace_tools.edit_file,
            workspace_tools.list_files,
            workspace_tools.grep_files,
            workspace_tools.shell,
            workspace_tools.read_process_output,
            workspace_tools.kill_process,
        ]
    ) == [
        "edit_file",
        "grep_files",
        "kill_process",
        "list_files",
        "read_file",
        "read_process_output",
        "shell",
        "view_image",
        "write_file",
    ]


async def test_plugin_tools(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    assert {
        "todo": await _plugin_names(Todo()),
        "skills": await _plugin_names(Skills(tmp_path / "skills")),
        "memory": await _plugin_names(Memory(tmp_path / "memory")),
        "subagents": await _plugin_names(Subagents()),
    } == {
        "todo": ["todo_write"],
        "skills": ["load_skill", "read_skill_file"],
        "memory": ["forget", "recall", "remember"],
        "subagents": [
            "cancel_subagent",
            "send_to_subagent",
            "spawn_subagent",
            "wait_subagents",
        ],
    }


async def test_mcp_deferred_tools() -> None:
    pytest.importorskip("mcp")
    from lovia.plugins.mcp import MCP, MCPServerStdio

    server = MCPServerStdio(command=sys.executable, args=[str(_MCP_STUB)], defer=True)
    assert await _plugin_names(MCP(server)) == ["call_mcp_tool", "search_mcp_tools"]


async def test_web_tools() -> None:
    pytest.importorskip("fastapi")
    from lovia.web import ChatStore, Scheduling, show_chart, show_html, show_table
    from lovia.web.vision import make_describe_image_tool

    # describe_image is the CLI's vision tool, added when a vision model is set.
    vision = make_describe_image_tool(ScriptedProvider([text("x")]))
    assert _names([show_chart, show_table, show_html, vision]) == [
        "describe_image",
        "show_chart",
        "show_html",
        "show_table",
    ]
    assert await _plugin_names(Scheduling(ChatStore.in_memory())) == [
        "cancel_schedule",
        "list_schedules",
        "schedule_run",
    ]
