"""Direct invocation of the workspace file tools (renderers included)."""

from __future__ import annotations

import pytest

from lovia.exceptions import ToolError
from lovia.run_context import RunContext
from lovia.tools import (
    render_tool_result,
)
from lovia.workspace import LocalWorkspaceSession, WorkspaceLimits
from lovia.workspace.types import (
    CommandResult,
    DirEntry,
    EditResult,
    FileChange,
    GrepMatch,
)
from lovia.workspace.tools import (
    read_file,
    write_file,
    edit_file,
    list_files,
    grep_files,
    shell,
    _render_command_result,
    _render_edit_result,
    _render_entries,
    _render_file_change,
    _render_matches,
    _shell_needs_approval,
)


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
async def test_write_and_edit_renderers_are_human_readable(session, tmp_path) -> None:
    ctx = _ctx(session)
    created = await write_file.invoke({"path": "new.txt", "content": "hi"}, ctx)
    assert (
        await render_tool_result(write_file, created, ctx) == "created new.txt (2 bytes)"
    )

    raw = await edit_file.invoke({"path": "a.txt", "old": "beta", "new": "BETA"}, ctx)
    assert await render_tool_result(edit_file, raw, ctx) == "edited a.txt (1 replacement)"

    nochange = await edit_file.invoke(
        {"path": "a.txt", "old": "BETA", "new": "BETA"}, ctx
    )
    assert "no change" in await render_tool_result(edit_file, nochange, ctx)
    missing = await edit_file.invoke({"path": "a.txt", "old": "zzz", "new": "x"}, ctx)
    assert "not found" in await render_tool_result(edit_file, missing, ctx)


@pytest.mark.asyncio
async def test_list_and_grep_truncate_with_a_note(tmp_path) -> None:
    for i in range(8):
        (tmp_path / f"f{i}.txt").write_text("hit\nhit\n", encoding="utf-8")
    session = LocalWorkspaceSession(
        root=str(tmp_path), limits=WorkspaceLimits(max_list_results=3)
    )
    ctx = _ctx(session)

    listed = await list_files.invoke({}, ctx)
    assert len(listed) == 3  # capped, not an error
    assert "truncated at 3 entries" in await render_tool_result(list_files, listed, ctx)

    matches = await grep_files.invoke({"pattern": "hit", "max_matches": 4}, ctx)
    assert len(matches) == 4
    assert "truncated at 4 matches" in await render_tool_result(grep_files, matches, ctx)


@pytest.mark.asyncio
async def test_grep_files_include_hidden(tmp_path) -> None:
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")
    (tmp_path / "app.py").write_text("TOKEN=x", encoding="utf-8")
    ctx = _ctx(LocalWorkspaceSession(root=str(tmp_path)))
    default = await grep_files.invoke({"pattern": "TOKEN"}, ctx)
    assert [m.path for m in default] == ["app.py"]
    incl = await grep_files.invoke({"pattern": "TOKEN", "include_hidden": True}, ctx)
    assert {m.path for m in incl} == {".env", "app.py"}


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


@pytest.mark.asyncio
async def test_read_file_renders_empty_and_past_eof(tmp_path) -> None:
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    (tmp_path / "small.txt").write_text("a\nb\n", encoding="utf-8")
    ctx = _ctx(LocalWorkspaceSession(root=str(tmp_path)))

    empty = await read_file.invoke({"path": "empty.txt"}, ctx)
    assert await render_tool_result(read_file, empty, ctx) == "empty.txt (empty file)"

    past = await read_file.invoke({"path": "small.txt", "start": 99}, ctx)
    rendered = await render_tool_result(read_file, past, ctx)
    assert "past the last line" in rendered and "(2)" in rendered


@pytest.mark.asyncio
async def test_read_file_renders_oversized_note(tmp_path) -> None:
    (tmp_path / "huge.txt").write_text(
        "\n".join(f"line{i}" for i in range(1, 2001)), encoding="utf-8"
    )
    session = LocalWorkspaceSession(
        root=str(tmp_path), limits=WorkspaceLimits(max_file_read_bytes=200)
    )
    ctx = _ctx(session)
    raw = await read_file.invoke({"path": "huge.txt"}, ctx)
    # A partial read must be visible to the model even when not char-clipped.
    assert "leading portion" in await render_tool_result(read_file, raw, ctx)


@pytest.mark.asyncio
async def test_edit_file_refuses_non_utf8(tmp_path) -> None:
    (tmp_path / "bin.txt").write_bytes(b"caf\xe9 x\n")
    ctx = _ctx(LocalWorkspaceSession(root=str(tmp_path)))
    raw = await edit_file.invoke({"path": "bin.txt", "old": "x", "new": "y"}, ctx)
    assert raw.ok is False
    assert "UTF-8" in await render_tool_result(edit_file, raw, ctx)
    assert (tmp_path / "bin.txt").read_bytes() == b"caf\xe9 x\n"


@pytest.mark.asyncio
async def test_grep_tool_honors_workspace_limit(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("hit\n" * 8, encoding="utf-8")
    session = LocalWorkspaceSession(
        root=str(tmp_path), limits=WorkspaceLimits(max_grep_matches=3)
    )
    ctx = _ctx(session)
    # No explicit max_matches -> the workspace limit applies (it was ignored
    # while the tool hardcoded a default).
    matches = await grep_files.invoke({"pattern": "hit"}, ctx)
    assert len(matches) == 3


# ---------------------------------------------------------------------------
# Renderers: type guards and edge messages (pure functions)
# ---------------------------------------------------------------------------


def test_render_entries_passes_through_non_entry_results() -> None:
    ctx = _ctx(None)
    # Not a list of DirEntry -> returned unchanged for the default renderer.
    assert _render_entries("already a string", ctx) == "already a string"


def test_render_entries_empty_and_size_variants() -> None:
    ctx = _ctx(None)
    assert _render_entries([], ctx) == "(no entries)"
    out = _render_entries(
        [
            DirEntry(path="dir", is_dir=True),
            DirEntry(path="big.txt", is_dir=False, size=12),
            DirEntry(path="nosize", is_dir=False, size=None),
        ],
        ctx,
    )
    assert out == "dir/\nbig.txt  (12 bytes)\nnosize"


def test_render_matches_passes_through_non_matches() -> None:
    assert _render_matches(42, _ctx(None)) == 42
    assert _render_matches([GrepMatch(path="f", line=1, text="x")], _ctx(None)) == "f:1: x"


def test_render_file_change_guard_and_messages() -> None:
    ctx = _ctx(None)
    assert _render_file_change("raw", ctx) == "raw"  # not a FileChange
    failed = FileChange(ok=False, path="f", action="created", message="boom")
    assert _render_file_change(failed, ctx) == "boom"
    unchanged = FileChange(ok=True, path="f.txt", action="unchanged")
    assert _render_file_change(unchanged, ctx) == "f.txt unchanged"


def test_render_edit_result_guard() -> None:
    assert _render_edit_result(["not", "an", "edit"], _ctx(None)) == ["not", "an", "edit"]
    failed = EditResult(ok=False, path="f", message="edit failed")
    assert _render_edit_result(failed, _ctx(None)) == "edit failed"


def test_render_command_result_timeout() -> None:
    res = CommandResult(exit_code=None, stdout="", stderr="killed", timed_out=True)
    assert _render_command_result(res, _ctx(None)) == "command timed out\nkilled"


# ---------------------------------------------------------------------------
# Shell approval gate (fail-closed)
# ---------------------------------------------------------------------------


def test_shell_needs_approval_fails_closed_without_workspace() -> None:
    assert _shell_needs_approval({"command": "ls"}, _ctx(None)) is True


def test_shell_needs_approval_fails_closed_on_bad_args(session) -> None:
    # Missing / non-string command -> ask rather than run something unjudged.
    assert _shell_needs_approval({}, _ctx(session)) is True
    assert _shell_needs_approval({"command": 123}, _ctx(session)) is True
