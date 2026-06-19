"""Built-in workspace file & shell tools.

These tool definitions live in the workspace package because they are
intrinsically workspace-scoped: each resolves the active
:class:`~lovia.workspace.protocol.WorkspaceSession` from
``RunContext.workspace`` — the runner injects it when the agent has a
``workspace=`` configured — and delegates to it. The session enforces the
workspace policy's path and command rules, so file/shell access is always
confined; there is no unconfined variant.

The module depends only on :mod:`lovia.tools.base` (the ``@tool``
infrastructure), keeping the package dependency one-directional:
``workspace -> tools.base``.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..exceptions import ToolError
from ..run_context import RunContext
from ..tools.base import tool
from .protocol import WorkspaceSession
from .types import CommandResult, DirEntry, FileContent, GrepMatch

__all__ = [
    "edit_file",
    "grep_files",
    "list_files",
    "read_file",
    "require_workspace",
    "shell",
    "write_file",
]


def require_workspace(ctx: RunContext[Any]) -> WorkspaceSession:
    """Return the active workspace session or fail with a setup hint."""
    workspace = ctx.workspace
    if workspace is None:
        raise ToolError(
            "No workspace is configured for this run.",
            hint="Set Agent(workspace=Workspace.local('path/to/root')) to enable file tools.",
        )
    return workspace


# ---------------------------------------------------------------------------
# Renderers — the strings the model actually sees
# ---------------------------------------------------------------------------


def _render_file_content(result: Any, ctx: RunContext[Any]) -> Any:
    if not isinstance(result, FileContent):
        return result
    header = (
        f"{result.path} (lines {result.start}-{result.end} of {result.total_lines})"
    )
    if not result.content:
        return f"{header}\n(empty)"
    return f"{header}\n{result.content}"


def _render_entries(result: Any, ctx: RunContext[Any]) -> Any:
    if not isinstance(result, list) or not all(
        isinstance(item, DirEntry) for item in result
    ):
        return result
    if not result:
        return "(no entries)"
    lines = []
    for entry in result:
        if entry.is_dir:
            lines.append(f"{entry.path}/")
        elif entry.size is not None:
            lines.append(f"{entry.path}  ({entry.size} bytes)")
        else:
            lines.append(entry.path)
    return "\n".join(lines)


def _render_matches(result: Any, ctx: RunContext[Any]) -> Any:
    if not isinstance(result, list) or not all(
        isinstance(item, GrepMatch) for item in result
    ):
        return result
    if not result:
        return "(no matches)"
    return "\n".join(f"{m.path}:{m.line}: {m.text}" for m in result)


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------


@tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file from the workspace.\n"
        "- path is workspace-relative (e.g. 'src/app.py'); absolute paths are rejected.\n"
        "- Large files are truncated; use start/end (1-based line numbers, inclusive) "
        "to read in pages.\n"
        "- Always read a file before editing it so edit_file gets exact text.\n"
        "- Binary files are not supported."
    ),
    result_renderer=_render_file_content,
)
async def read_file(
    ctx: RunContext[Any],
    path: Annotated[str, "Workspace-relative file path."],
    start: Annotated[
        int | None, Field(default=None, ge=1, description="1-based start line.")
    ] = None,
    end: Annotated[
        int | None,
        Field(default=None, ge=1, description="1-based inclusive end line."),
    ] = None,
) -> FileContent:
    return await require_workspace(ctx).read_text(path, start=start, end=end)


# TODO write_file, edit_file不需要result_renderer吗?
@tool(
    name="write_file",
    description=(
        "Create or overwrite a UTF-8 text file in the workspace.\n"
        "- Writes the full content; parent directories are created automatically.\n"
        "- Prefer edit_file for targeted changes to an existing file — write_file "
        "replaces the whole file and loses anything you did not include.\n"
        "- Set create_only=true to fail instead of overwriting an existing file."
    ),
)
async def write_file(
    ctx: RunContext[Any],
    path: Annotated[str, "Workspace-relative file path."],
    content: Annotated[str, "Full file content to write."],
    create_only: Annotated[
        bool,
        Field(default=False, description="If true, never overwrite an existing file."),
    ] = False,
) -> object:
    return await require_workspace(ctx).write_text(
        path, content, create_only=create_only
    )


@tool(
    name="edit_file",
    description=(
        "Replace exact text in a workspace file.\n"
        "- old must match the file content exactly, including whitespace and "
        "indentation — read_file first and copy the span verbatim.\n"
        "- Fails if old matches zero times, or multiple times without "
        "replace_all; on a multi-match failure, include more surrounding "
        "context to make the span unique.\n"
        "- Set replace_all=true to replace every occurrence (e.g. renaming a "
        "symbol across one file)."
    ),
)
async def edit_file(
    ctx: RunContext[Any],
    path: Annotated[str, "Workspace-relative file path."],
    old: Annotated[str, "Exact existing text to replace."],
    new: Annotated[str, "Replacement text."],
    replace_all: Annotated[
        bool,
        Field(default=False, description="Replace every occurrence of old."),
    ] = False,
) -> object:
    return await require_workspace(ctx).edit_text(
        path, old, new, replace_all=replace_all
    )


@tool(
    name="list_files",
    description=(
        "List files and directories in the workspace.\n"
        "- Without pattern: lists the direct children of path (directories "
        "end with '/').\n"
        "- With pattern: returns paths matching a glob relative to path, e.g. "
        "'**/*.py' for all Python files recursively.\n"
        "- Hidden files (dotfiles) are skipped unless include_hidden=true.\n"
        "- To search file *contents*, use grep_files instead."
    ),
    result_renderer=_render_entries,
)
async def list_files(
    ctx: RunContext[Any],
    path: Annotated[str, "Workspace-relative directory path."] = ".",
    pattern: Annotated[
        str | None,
        Field(default=None, description="Optional glob pattern, e.g. '**/*.py'."),
    ] = None,
    include_hidden: Annotated[
        bool, Field(default=False, description="Include dotfiles/directories.")
    ] = False,
) -> list[DirEntry]:
    return await require_workspace(ctx).list_files(
        path, pattern=pattern, include_hidden=include_hidden
    )


@tool(
    name="grep_files",
    description=(
        "Search file contents in the workspace with a regular expression "
        "(Python re syntax).\n"
        "- Returns matching lines as 'path:line: text', capped at max_matches.\n"
        "- Scope the search with path (directory) and/or glob (filename "
        "pattern, e.g. '*.py').\n"
        "- Hidden paths and binary files are skipped.\n"
        "- This is the fastest way to locate code or text; prefer it over "
        "reading files one by one."
    ),
    result_renderer=_render_matches,
)
async def grep_files(
    ctx: RunContext[Any],
    pattern: Annotated[str, "Regular expression to search for."],
    path: Annotated[str, "Workspace-relative directory to search."] = ".",
    glob: Annotated[
        str | None,
        Field(
            default=None,
            description="Only search files matching this glob, e.g. '*.py'.",
        ),
    ] = None,
    ignore_case: Annotated[
        bool, Field(default=False, description="Case-insensitive matching.")
    ] = False,
    max_matches: Annotated[
        int, Field(default=100, ge=1, le=1000, description="Maximum matches returned.")
    ] = 100,
) -> list[GrepMatch]:
    return await require_workspace(ctx).grep(
        pattern,
        path=path,
        glob=glob,
        ignore_case=ignore_case,
        max_matches=max_matches,
    )


# ---------------------------------------------------------------------------
# Shell tool
# ---------------------------------------------------------------------------


def _shell_needs_approval(args: dict[str, Any], ctx: RunContext[Any]) -> bool:
    workspace = ctx.workspace
    # TODO 返回false的意思是不需要approval（see lovia/runtime/tool_calls.py），直接放行，怎么这里有例外？
    if workspace is None:
        return False  # the tool itself will fail with a setup hint
    command = args.get("command")
    if not isinstance(command, str):
        return False  # malformed args fail validation later
    return workspace.policy.decide_command(command) == "ask"


def _render_command_result(result: Any, ctx: RunContext[Any]) -> Any:
    if not isinstance(result, CommandResult):
        return result
    if result.timed_out:
        return f"command timed out\n{result.stderr}".strip()
    parts = [f"exit code: {result.exit_code}"]
    if result.stdout.strip():
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr.strip():
        parts.append(f"--- stderr ---\n{result.stderr.rstrip(chr(10))}")
    if len(parts) == 1:
        parts.append("(no output)")
    return "\n".join(parts)


@tool(
    name="shell",
    description=(
        "Run a one-shot, non-interactive shell command in the workspace.\n"
        "- cwd is workspace-relative; the command starts there. Quote paths "
        "that contain spaces.\n"
        "- No TTY and no interactive input: never run editors, REPLs, "
        "watchers, or anything that prompts (use non-interactive flags like "
        "--yes instead). Long-running commands are killed at the timeout.\n"
        "- stdout/stderr are captured and truncated when large; pipe through "
        "filters (grep, head, tail) to keep output focused.\n"
        "- The command runs as the host user, so it is NOT sandboxed: "
        "destructive or out-of-workspace commands may be denied by policy or "
        "require user approval. Never run destructive commands (rm -rf, "
        "git reset --hard, force-push, ...) unless the user explicitly asked.\n"
        "- Prefer the dedicated tools over shell equivalents: read_file over "
        "cat, grep_files over grep/rg, list_files over ls/find, edit_file "
        "over sed."
    ),
    needs_approval=_shell_needs_approval,
    result_renderer=_render_command_result,
)
async def shell(
    ctx: RunContext[Any],
    command: Annotated[str, "Shell command line to run."],
    cwd: Annotated[str, "Workspace-relative working directory."] = ".",
    timeout: Annotated[
        float | None,
        Field(default=None, ge=1, description="Override timeout in seconds."),
    ] = None,
) -> CommandResult:
    return await require_workspace(ctx).run(command, cwd=cwd, timeout=timeout)
