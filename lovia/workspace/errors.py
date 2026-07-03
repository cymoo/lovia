"""Workspace exception hierarchy."""

from __future__ import annotations

from ..exceptions import ToolError

__all__ = [
    "PermissionDeniedError",
    "WorkspaceClosedError",
    "WorkspaceError",
]


class WorkspaceError(ToolError):
    """Base class for workspace-layer failures."""


class PermissionDeniedError(WorkspaceError):
    """Raised when the workspace policy rejects an operation."""


class WorkspaceClosedError(WorkspaceError):
    """Raised when a closed session is used."""
