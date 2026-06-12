"""Workspace exception hierarchy."""

from __future__ import annotations

from ..exceptions import ToolError

__all__ = [
    "PathOutsideWorkspaceError",
    "PermissionDeniedError",
    "WorkspaceBackendError",
    "WorkspaceClosedError",
    "WorkspaceError",
]


class WorkspaceError(ToolError):
    """Base class for workspace-layer failures."""


class PathOutsideWorkspaceError(WorkspaceError):
    """Raised when a path escapes the workspace root."""


class PermissionDeniedError(WorkspaceError):
    """Raised when the workspace policy rejects an operation."""


class WorkspaceBackendError(WorkspaceError):
    """Raised when a workspace backend cannot open or operate."""


class WorkspaceClosedError(WorkspaceError):
    """Raised when a closed session is used."""
