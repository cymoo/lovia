"""Exception hierarchy for workspace tools."""

from __future__ import annotations

from ..exceptions import ToolError

__all__ = [
    "AuditBlocked",
    "ExecTimeout",
    "PathEscape",
    "WorkspaceClosed",
    "WorkspaceError",
]


class WorkspaceError(ToolError):
    """Base class for workspace-layer failures."""


class PathEscape(WorkspaceError):
    """Raised when a path resolves outside the workspace root."""


class WorkspaceClosed(WorkspaceError):
    """Raised when a method is invoked on a closed workspace."""


class ExecTimeout(WorkspaceError):
    """Raised by callers that prefer exceptions over timed-out results."""


class AuditBlocked(WorkspaceError):
    """Raised when an audit policy blocks a command."""
