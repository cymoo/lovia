"""Workspaces: scoped filesystem/shell surfaces for agent tools.

A workspace gives an agent's file and shell tools a root directory and a
permission policy. Paths and shell commands share one three-valued ACL
(allow / ask / deny): the local backend enforces ``deny`` in the session,
routes ``ask`` through the human-approval channel, and judges shell commands
with the same path rules via a lexical guard; hard OS isolation is the job
of sandboxing executors or future backends implementing the same protocols.
"""

from __future__ import annotations

from .errors import (
    PermissionDeniedError,
    WorkspaceClosedError,
    WorkspaceError,
)
from .local import LocalWorkspaceSession
from .policy import CommandRule, Decision, FileOp, PathRule, WorkspacePolicy

# ``WorkspaceLike`` / ``WorkspaceSession`` / ``ShellExecutor`` are the
# extension protocols for authoring a custom backend or sandboxing executor;
# they stay importable from here (and from ``lovia.workspace.protocol``) but
# are kept out of ``__all__`` so the advertised surface is the handful of
# types day-to-day users actually touch.
from .protocol import ShellExecutor, WorkspaceLike, WorkspaceSession  # noqa: F401
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
from .workspace import LocalWorkspace, Workspace

__all__ = [
    "CommandResult",
    "CommandRule",
    "Decision",
    "DirEntry",
    "EditResult",
    "FileChange",
    "FileContent",
    "FileOp",
    "GrepMatch",
    "LocalWorkspace",
    "LocalWorkspaceSession",
    "PathRule",
    "PermissionDeniedError",
    "Workspace",
    "WorkspaceClosedError",
    "WorkspaceError",
    "WorkspaceLimits",
    "WorkspaceMode",
    "WorkspacePolicy",
]
