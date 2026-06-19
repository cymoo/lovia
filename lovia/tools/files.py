"""Backward-compatible re-exports for the workspace file tools.

The file tools are workspace-scoped, so their definitions now live in
:mod:`lovia.workspace.tools`. They remain importable from here (and from
:mod:`lovia.tools`) for compatibility; new code should prefer
``Agent(workspace=Workspace.local(...))``, which adds them automatically.
"""

from __future__ import annotations

from ..workspace.tools import (
    edit_file,
    grep_files,
    list_files,
    read_file,
    require_workspace,
    write_file,
)

__all__ = [
    "edit_file",
    "grep_files",
    "list_files",
    "read_file",
    "require_workspace",
    "write_file",
]
