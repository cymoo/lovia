"""Agent-facing workspace tool factories."""

from __future__ import annotations

from typing import Annotated, cast

from pydantic import Field

from ..tools import Tool, tool
from .audit import AuditPolicy, AuditStream, AuditToolPolicy, default_audit_policy
from .types import DirEntry, ExecLimits, ExecResult
from .workspace import Workspace, default_workspace

__all__ = ["bash", "edit_file", "glob", "list_dir", "read_file", "write_file"]

_DEFAULT_AUDIT = object()


def _workspace(workspace: Workspace | None) -> Workspace:
    return workspace if workspace is not None else default_workspace()


def _result_dict(result: ExecResult) -> dict[str, object]:
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "truncated": result.truncated,
    }


def _entry_dict(entry: DirEntry) -> dict[str, object]:
    return {
        "name": entry.name,
        "is_dir": entry.is_dir,
        "size": entry.size,
        "mtime": entry.mtime,
    }


def bash(
    workspace: Workspace | None = None,
    *,
    audit: AuditPolicy | None | object = _DEFAULT_AUDIT,
    audit_stream: AuditStream | None = None,
    limits: ExecLimits | None = None,
) -> Tool:
    """Create a ``bash`` tool for running commands in a workspace."""

    ws = _workspace(workspace)
    base_limits = limits or ExecLimits()
    policies = []
    if audit is _DEFAULT_AUDIT:
        policies.append(AuditToolPolicy(default_audit_policy(), audit_stream))
    elif audit is not None:
        policies.append(AuditToolPolicy(cast(AuditPolicy, audit), audit_stream))

    @tool(
        name="bash",
        description=(
            "Run a shell command in the workspace. The local workspace is not "
            "a security boundary; commands run as the host user."
        ),
        timeout=(base_limits.timeout + 5) if base_limits.timeout is not None else None,
        policies=tuple(policies),
    )
    async def _bash(
        command: Annotated[str, "Shell command to run."],
        cwd: Annotated[
            str,
            "Workspace-relative cwd, or /workspace/... logical absolute path.",
        ] = ".",
        timeout: Annotated[
            float | None,
            Field(default=None, description="Override timeout in seconds."),
        ] = None,
    ) -> dict[str, object]:
        per_call_limits = ExecLimits(
            timeout=base_limits.timeout if timeout is None else timeout,
            max_output_bytes=base_limits.max_output_bytes,
        )
        return _result_dict(await ws.run(command, cwd=cwd, limits=per_call_limits))

    return _bash


def read_file(workspace: Workspace | None = None) -> Tool:
    """Create a ``read_file`` tool."""

    ws = _workspace(workspace)

    @tool(
        name="read_file",
        description="Read a UTF-8 file from the workspace as raw text.",
    )
    async def _read_file(
        path: Annotated[str, "Workspace-relative path or /workspace/... path."],
        start_line: Annotated[
            int | None,
            Field(default=None, ge=1, description="1-based start line."),
        ] = None,
        max_lines: Annotated[
            int | None,
            Field(default=None, ge=1, description="Maximum lines to return."),
        ] = None,
        max_bytes: Annotated[
            int | None,
            Field(default=None, ge=1, description="Maximum bytes to read."),
        ] = None,
    ) -> str:
        return await ws.read_file(
            path, start_line=start_line, max_lines=max_lines, max_bytes=max_bytes
        )

    return _read_file


def write_file(workspace: Workspace | None = None) -> Tool:
    """Create a ``write_file`` tool."""

    ws = _workspace(workspace)

    @tool(
        name="write_file",
        description=(
            "Write a UTF-8 file inside the workspace. Writes affect real files "
            "for local workspaces."
        ),
    )
    async def _write_file(
        path: Annotated[str, "Workspace-relative path or /workspace/... path."],
        content: Annotated[str, "Full file content to write."],
        append: Annotated[
            bool, Field(default=False, description="Append instead of replacing.")
        ] = False,
        overwrite: Annotated[
            bool,
            Field(default=True, description="If false, fail when the file exists."),
        ] = True,
    ) -> dict[str, int]:
        return {
            "bytes_written": await ws.write_file(
                path, content, append=append, overwrite=overwrite
            )
        }

    return _write_file


def edit_file(workspace: Workspace | None = None) -> Tool:
    """Create an ``edit_file`` tool."""

    ws = _workspace(workspace)

    @tool(
        name="edit_file",
        description=(
            "Replace exact text in a workspace file. The old_text must match "
            "exactly; on failure, re-read the file before retrying."
        ),
    )
    async def _edit_file(
        path: Annotated[str, "Workspace-relative path or /workspace/... path."],
        old_text: Annotated[str, "Exact text to replace."],
        new_text: Annotated[str, "Replacement text."],
        replace_all: Annotated[
            bool,
            Field(default=False, description="Replace all matches instead of one."),
        ] = False,
    ) -> dict[str, int]:
        return {
            "replacements": await ws.edit_file(
                path, old_text, new_text, replace_all=replace_all
            )
        }

    return _edit_file


def glob(workspace: Workspace | None = None) -> Tool:
    """Create a ``glob`` tool."""

    ws = _workspace(workspace)

    @tool(
        name="glob",
        description=(
            "Find workspace paths matching a glob pattern. Hidden paths are "
            "skipped by default."
        ),
    )
    async def _glob(
        pattern: Annotated[str, "Glob pattern relative to the workspace."],
        include_hidden: Annotated[
            bool, Field(default=False, description="Include dotfiles/directories.")
        ] = False,
        max_results: Annotated[
            int, Field(default=1_000, ge=1, description="Maximum results.")
        ] = 1_000,
    ) -> list[str]:
        return await ws.glob(
            pattern, include_hidden=include_hidden, max_results=max_results
        )

    return _glob


def list_dir(workspace: Workspace | None = None) -> Tool:
    """Create a ``list_dir`` tool."""

    ws = _workspace(workspace)

    @tool(
        name="list_dir",
        description="List direct children of a workspace directory.",
    )
    async def _list_dir(
        path: Annotated[str, "Workspace-relative path or /workspace/... path."] = ".",
        include_hidden: Annotated[
            bool, Field(default=False, description="Include dotfiles/directories.")
        ] = False,
        max_results: Annotated[
            int, Field(default=1_000, ge=1, description="Maximum entries.")
        ] = 1_000,
    ) -> list[dict[str, object]]:
        return [
            _entry_dict(entry)
            for entry in await ws.list_dir(
                path, include_hidden=include_hidden, max_results=max_results
            )
        ]

    return _list_dir
