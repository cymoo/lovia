"""Built-in workspace file & shell tools.

These tool definitions live in the workspace package because they are
intrinsically workspace-scoped: each resolves the active
:class:`~lovia.workspace.protocol.WorkspaceSession` from
``RunContext.workspace`` — the runner injects it when the agent has a
``workspace=`` configured — and delegates to it. The session enforces the
workspace policy's path ACL and command rules on every operation (deny
raises there; the ``needs_approval`` predicates here resolve the ask side),
so there is no ungated variant of these tools.

The module depends only on :mod:`lovia.tools.base` (the ``@tool``
infrastructure), keeping the package dependency one-directional:
``workspace -> tools.base``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from ..exceptions import ToolError
from ..run_context import RunContext
from ..tools.base import tool
from .errors import WorkspaceError
from .protocol import WorkspaceSession
from .types import (
    CommandResult,
    DirEntry,
    EditResult,
    FileChange,
    FileContent,
    GrepMatch,
)

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


def _path_needs_approval(
    *ops: Literal["read", "write"],
) -> Callable[[dict[str, Any], RunContext[Any]], bool]:
    """Approval predicate for a path-taking tool.

    Returns True when the policy decision for the tool's ``path`` argument is
    ``ask`` for any of the given ops (and none of them is ``deny`` — a denied
    call fails at the session anyway, so asking the human first would be
    noise). With no workspace the tool cannot touch anything — let it run so
    ``require_workspace`` surfaces its setup hint instead of a confusing
    "not approved". With a live workspace but malformed args or a broken
    policy, fail closed: ask rather than run unjudged.
    """

    def _needs(args: dict[str, Any], ctx: RunContext[Any]) -> bool:
        workspace = ctx.workspace
        if workspace is None:
            return False
        path = args.get("path", ".")
        if not isinstance(path, str):
            return True
        try:
            decisions = [workspace.decide_path(path, write=op == "write") for op in ops]
        except Exception:  # noqa: BLE001 — never run unjudged on a bad policy
            return True
        if "deny" in decisions:
            return False
        return "ask" in decisions

    return _needs


# ---------------------------------------------------------------------------
# Renderers — the strings the model actually sees
# ---------------------------------------------------------------------------


def _render_file_content(result: Any, ctx: RunContext[Any]) -> Any:
    if not isinstance(result, FileContent):
        return result
    if result.total_lines == 0:
        return f"{result.path} (empty file)"
    if not result.content:
        # The requested start is past the last line — don't print a backwards
        # range like "lines 1000-10 of 10".
        return (
            f"{result.path}: start line {result.start} is past the last line "
            f"({result.total_lines})."
        )
    # Truncation is already visible: end < total_lines (more to page), the clip
    # notice inside content, or the oversized note appended by read_text.
    header = (
        f"{result.path} (lines {result.start}-{result.end} of {result.total_lines})"
    )
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
        suffix = ""
        if entry.symlink_target is not None:
            suffix = f"  -> {entry.symlink_target}"
        if entry.is_dir:
            lines.append(f"{entry.path}/{suffix}")
        elif entry.size is not None:
            lines.append(f"{entry.path}  ({entry.size} bytes){suffix}")
        else:
            lines.append(f"{entry.path}{suffix}")
    if getattr(result, "truncated", False):
        lines.append(
            f"… (truncated at {len(result)} entries; narrow the path/pattern "
            "or raise max_results)"
        )
    return "\n".join(lines)


def _render_matches(result: Any, ctx: RunContext[Any]) -> Any:
    if not isinstance(result, list) or not all(
        isinstance(item, GrepMatch) for item in result
    ):
        return result
    if not result:
        return "(no matches)"
    lines = [f"{m.path}:{m.line}: {m.text}" for m in result]
    if getattr(result, "truncated", False):
        lines.append(
            f"… (truncated at {len(result)} matches; narrow the search "
            "or raise max_matches)"
        )
    return "\n".join(lines)


def _render_file_change(result: Any, ctx: RunContext[Any]) -> Any:
    if not isinstance(result, FileChange):
        return result
    if not result.ok:
        return result.message or "no change"
    if result.action == "unchanged":
        return result.message or f"{result.path} unchanged"
    return f"{result.action} {result.path} ({result.bytes_written} bytes)"


def _render_edit_result(result: Any, ctx: RunContext[Any]) -> Any:
    if not isinstance(result, EditResult):
        return result
    if not result.ok:
        return result.message or "edit failed"
    if not result.changed:
        return f"{result.path}: no change (old text == new text)"
    plural = "" if result.replacements == 1 else "s"
    return f"edited {result.path} ({result.replacements} replacement{plural})"


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------


@tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file.\n"
        "- path is workspace-relative (e.g. 'src/app.py') or absolute; paths "
        "outside the workspace may require user approval or be denied by "
        "policy.\n"
        "- Large files are truncated; use start/end (1-based line numbers, inclusive) "
        "to read in pages.\n"
        "- Always read a file before editing it so edit_file gets exact text.\n"
        "- Binary files decode to replacement characters and aren't useful here."
    ),
    needs_approval=_path_needs_approval("read"),
    result_renderer=_render_file_content,
)
async def read_file(
    ctx: RunContext[Any],
    path: Annotated[str, "Workspace-relative or absolute file path."],
    start: Annotated[
        int | None, Field(default=None, ge=1, description="1-based start line.")
    ] = None,
    end: Annotated[
        int | None,
        Field(default=None, ge=1, description="1-based inclusive end line."),
    ] = None,
) -> FileContent:
    return await require_workspace(ctx).read_text(path, start=start, end=end)


@tool(
    name="write_file",
    description=(
        "Create or overwrite a UTF-8 text file.\n"
        "- path is workspace-relative or absolute; writes outside the "
        "workspace are usually denied or need user approval.\n"
        "- Writes the full content; parent directories are created automatically.\n"
        "- Prefer edit_file for targeted changes to an existing file — write_file "
        "replaces the whole file and loses anything you did not include.\n"
        "- Generating very large content in one call is slow and may be cut off "
        "mid-stream; split large output into multiple files, or write the file's "
        "skeleton first and extend it with edit_file.\n"
        "- Set create_only=true to fail instead of overwriting an existing file."
    ),
    needs_approval=_path_needs_approval("write"),
    result_renderer=_render_file_change,
    # Mutates the shared workspace: run as an execution barrier so two writes
    # (or a write racing a shell command) cannot interleave within one turn.
    parallel=False,
)
async def write_file(
    ctx: RunContext[Any],
    path: Annotated[str, "Workspace-relative or absolute file path."],
    content: Annotated[str, "Full file content to write."],
    create_only: Annotated[
        bool,
        Field(default=False, description="If true, never overwrite an existing file."),
    ] = False,
) -> FileChange:
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
    needs_approval=_path_needs_approval("read", "write"),
    result_renderer=_render_edit_result,
    # Mutates the shared workspace — barrier, same as write_file.
    parallel=False,
)
async def edit_file(
    ctx: RunContext[Any],
    path: Annotated[str, "Workspace-relative or absolute file path."],
    old: Annotated[str, "Exact existing text to replace."],
    new: Annotated[str, "Replacement text."],
    replace_all: Annotated[
        bool,
        Field(default=False, description="Replace every occurrence of old."),
    ] = False,
) -> EditResult:
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
        "- Symlinks are shown with their resolved target ('link -> /target').\n"
        "- To search file *contents*, use grep_files instead."
    ),
    needs_approval=_path_needs_approval("read"),
    result_renderer=_render_entries,
)
async def list_files(
    ctx: RunContext[Any],
    path: Annotated[
        str,
        "Directory to list, workspace-relative or absolute ('.' is the workspace root).",
    ] = ".",
    pattern: Annotated[
        str | None,
        Field(default=None, description="Optional glob pattern, e.g. '**/*.py'."),
    ] = None,
    include_hidden: Annotated[
        bool, Field(default=False, description="Include dotfiles/directories.")
    ] = False,
    max_results: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            le=1000,
            description="Maximum results returned (defaults to the workspace limit, 500).",
        ),
    ] = None,
) -> list[DirEntry]:
    session = require_workspace(ctx)
    try:
        return await session.list_files(
            path,
            pattern=pattern,
            include_hidden=include_hidden,
            max_results=max_results,
        )
    except WorkspaceError as exc:
        # Models sometimes address the root by the workspace's *name*. The
        # miss round-trips anyway, so make the retry a certainty: put the
        # actual root path in the error.
        root = getattr(session, "root", None)
        root_name = Path(root).expanduser().resolve().name if root else None
        if exc.hint is None and root_name and path.strip().strip("/") == root_name:
            exc.hint = (
                f"{root_name!r} is the workspace's name, not a path inside it "
                "— the root itself is '.'; try list_files('.')."
            )
        raise


@tool(
    name="grep_files",
    description=(
        "Search file contents in the workspace with a regular expression "
        "(Python re syntax).\n"
        "- Returns matching lines as 'path:line: text', capped at max_matches.\n"
        "- Scope the search with path (directory) and/or glob (filename "
        "pattern, e.g. '*.py').\n"
        "- path may be a directory to search recursively, or a single file.\n"
        "- Dotfiles are skipped unless include_hidden=true; binary files and "
        "policy-denied paths are always skipped.\n"
        "- This is the fastest way to locate code or text; prefer it over "
        "reading files one by one."
    ),
    needs_approval=_path_needs_approval("read"),
    result_renderer=_render_matches,
)
async def grep_files(
    ctx: RunContext[Any],
    pattern: Annotated[str, "Regular expression to search for."],
    path: Annotated[str, "Directory (or single file) to search."] = ".",
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
    include_hidden: Annotated[
        bool, Field(default=False, description="Also search dotfiles/dirs.")
    ] = False,
    max_matches: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            le=1000,
            description="Maximum matches returned (defaults to the workspace limit, 100).",
        ),
    ] = None,
) -> list[GrepMatch]:
    return await require_workspace(ctx).grep(
        pattern,
        path=path,
        glob=glob,
        ignore_case=ignore_case,
        include_hidden=include_hidden,
        max_matches=max_matches,
    )


# ---------------------------------------------------------------------------
# Shell tool
# ---------------------------------------------------------------------------


def _shell_needs_approval(args: dict[str, Any], ctx: RunContext[Any]) -> bool:
    # With no workspace nothing can run — require_workspace raises its setup
    # hint, so don't hide it behind an approval prompt. With a workspace but
    # malformed args or a broken policy, fail closed: ask rather than run
    # unjudged (validation then fails after approval; nothing runs unasked).
    workspace = ctx.workspace
    if workspace is None:
        return False
    command = args.get("command")
    cwd = args.get("cwd", ".")
    if not isinstance(command, str) or not isinstance(cwd, str):
        return True
    try:
        # The session's combined verdict: static command rules merged with
        # the path ACL over the command's path claims and working directory.
        return workspace.decide_command(command, cwd) == "ask"
    except Exception:  # noqa: BLE001 — never run unjudged on a bad policy
        return True


def _render_command_result(result: Any, ctx: RunContext[Any]) -> Any:
    if not isinstance(result, CommandResult):
        return result
    if result.timed_out:
        return f"command timed out\n{result.stderr}".strip()
    parts = [f"exit code: {result.exit_code}"]
    if result.stdout.strip():
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr.strip():
        # Hoisted to a local: a backslash inside an f-string expression is a
        # SyntaxError on Python 3.10/3.11 (allowed only from 3.12 / PEP 701).
        stderr_body = result.stderr.rstrip("\n")
        parts.append(f"--- stderr ---\n{stderr_body}")
    if len(parts) == 1:
        parts.append("(no output)")
    return "\n".join(parts)


@tool(
    name="shell",
    description=(
        "Run a one-shot, non-interactive shell command in the workspace.\n"
        "- cwd is workspace-relative; the command starts there. Quote paths "
        "that contain spaces.\n"
        "- Each call is a fresh process: nothing persists between calls (a cd, "
        "an exported variable, or a background job does not carry over). Chain "
        "steps in one command with && (or set cwd=).\n"
        "- No TTY and no interactive input: never run editors, REPLs, "
        "watchers, or anything that prompts (use non-interactive flags like "
        "--yes instead). Long-running commands are killed at the timeout.\n"
        "- stdout/stderr are captured and truncated when large; pipe through "
        "filters (grep, head, tail) to keep output focused.\n"
        "- The same path policy that governs the file tools also governs "
        "commands: paths named in a command (arguments, redirect targets, "
        "cwd) are checked, so a command touching denied or out-of-workspace "
        "paths is denied or requires user approval — the shell is not a way "
        "around the file tools.\n"
        "- The command runs as the host user and is NOT OS-sandboxed. Never "
        "run destructive commands (rm -rf, git reset --hard, force-push, ...) "
        "unless the user explicitly asked.\n"
        "- Prefer the dedicated tools over shell equivalents: read_file over "
        "cat, grep_files over grep/rg, list_files over ls/find, edit_file "
        "over sed."
    ),
    needs_approval=_shell_needs_approval,
    result_renderer=_render_command_result,
    # A command can mutate anything in the workspace — barrier, so it never
    # races file tools or another command within one turn.
    parallel=False,
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
