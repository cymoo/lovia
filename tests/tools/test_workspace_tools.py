"""Direct invocation of the workspace file tools (renderers included)."""

from __future__ import annotations

import pytest

from lovia.exceptions import ToolError
from lovia.run_context import RunContext
from lovia.tools import (
    edit_file,
    grep_files,
    list_files,
    read_file,
    render_tool_result,
    shell,
    write_file,
)
from lovia.workspace import LocalWorkspaceSession


def _ctx(session: LocalWorkspaceSession | None = None) -> RunContext:
    return RunContext(
        context=None,
        entries=[],
        agent=None,
        workspace=session,  # type: ignore[arg-type]
    )


@pytest.fixture
def session(tmp_path) -> LocalWorkspaceSession:
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    return LocalWorkspaceSession(root=str(tmp_path))


@pytest.mark.asyncio
async def test_tools_require_workspace() -> None:
    ctx = _ctx(None)
    for t in (read_file, write_file, edit_file, list_files, grep_files, shell):
        args = {
            "read_file": {"path": "x"},
            "write_file": {"path": "x", "content": "y"},
            "edit_file": {"path": "x", "old": "a", "new": "b"},
            "list_files": {},
            "grep_files": {"pattern": "a"},
            "shell": {"command": "echo"},
        }[t.name]
        with pytest.raises(ToolError, match="No workspace is configured"):
            await t.invoke(args, ctx)


@pytest.mark.asyncio
async def test_read_file_renders_with_header(session) -> None:
    ctx = _ctx(session)
    raw = await read_file.invoke({"path": "a.txt", "start": 2, "end": 3}, ctx)
    rendered = await render_tool_result(read_file, raw, ctx)
    assert rendered.startswith("a.txt (lines 2-3 of 3)")
    assert "beta\ngamma" in rendered


@pytest.mark.asyncio
async def test_edit_and_write_round_trip(session, tmp_path) -> None:
    ctx = _ctx(session)
    result = await edit_file.invoke(
        {"path": "a.txt", "old": "beta", "new": "BETA"}, ctx
    )
    assert result.ok is True
    assert "BETA" in (tmp_path / "a.txt").read_text()

    created = await write_file.invoke({"path": "sub/new.txt", "content": "hi"}, ctx)
    assert created.action == "created"


@pytest.mark.asyncio
async def test_list_files_renderer_marks_dirs(session, tmp_path) -> None:
    (tmp_path / "pkg").mkdir()
    ctx = _ctx(session)
    raw = await list_files.invoke({}, ctx)
    rendered = await render_tool_result(list_files, raw, ctx)
    assert "pkg/" in rendered
    assert "a.txt" in rendered


@pytest.mark.asyncio
async def test_grep_files_renderer(session) -> None:
    ctx = _ctx(session)
    raw = await grep_files.invoke({"pattern": "beta"}, ctx)
    rendered = await render_tool_result(grep_files, raw, ctx)
    assert rendered == "a.txt:2: beta"

    empty = await grep_files.invoke({"pattern": "nothing-here"}, ctx)
    assert await render_tool_result(grep_files, empty, ctx) == "(no matches)"


@pytest.mark.asyncio
async def test_shell_renderer_formats_result(session) -> None:
    ctx = _ctx(session)
    raw = await shell.invoke({"command": "echo out && echo err 1>&2"}, ctx)
    rendered = await render_tool_result(shell, raw, ctx)
    assert rendered.startswith("exit code: 0")
    assert "out" in rendered
    assert "--- stderr ---" in rendered and "err" in rendered

    quiet = await shell.invoke({"command": "true"}, ctx)
    assert "(no output)" in await render_tool_result(shell, quiet, ctx)
