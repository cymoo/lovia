"""Workspaces: scoped filesystem/shell surfaces for agent tools.

A workspace gives an agent's file and shell tools a root directory and a
permission policy. The local backend confines file operations to the root
and gates shell commands through allow/ask/deny rules; hard OS isolation is
the job of future sandboxed backends implementing the same protocols.
"""

from __future__ import annotations

from .errors import (
    PathOutsideWorkspaceError,
    PermissionDeniedError,
    WorkspaceBackendError,
    WorkspaceClosedError,
    WorkspaceError,
)
from .local import LocalWorkspaceSession
from .policy import CommandRule, Decision, WorkspacePolicy
from .protocol import WorkspaceLike, WorkspaceSession
from .types import (
    CommandResult,
    DirEntry,
    EditResult,
    FileChange,
    FileContent,
    GrepMatch,
    WorkspaceLimits,
    WorkspaceMode,
)
from ..tools.base import clip_text
from .workspace import LocalWorkspace, Workspace

__all__ = [
    "CommandResult",
    "CommandRule",
    "Decision",
    "DirEntry",
    "EditResult",
    "FileChange",
    "FileContent",
    "GrepMatch",
    "LocalWorkspace",
    "LocalWorkspaceSession",
    "PathOutsideWorkspaceError",
    "PermissionDeniedError",
    "Workspace",
    "WorkspaceBackendError",
    "WorkspaceClosedError",
    "WorkspaceError",
    "WorkspaceLike",
    "WorkspaceLimits",
    "WorkspaceMode",
    "WorkspacePolicy",
    "WorkspaceSession",
    "clip_text",
]
