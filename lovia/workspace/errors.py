"""Workspace exception hierarchy."""

from __future__ import annotations

from ..exceptions import ToolError

__all__ = [
    "FileTooLargeError",
    "PermissionDeniedError",
    "WorkspaceClosedError",
    "WorkspaceDisconnectedError",
    "WorkspaceError",
]


class WorkspaceError(ToolError):
    """Base class for workspace-layer failures."""


class PermissionDeniedError(WorkspaceError):
    """Raised when the workspace policy rejects an operation."""


class WorkspaceClosedError(WorkspaceError):
    """Raised when a closed session is used."""


class WorkspaceDisconnectedError(WorkspaceError):
    """Raised when the connection to a remote workspace is lost.

    An operation in flight when it happened has an **unknown outcome** — it
    may have run. Nothing is retried or replayed; a later run reconnects and
    opens a fresh session.
    """


class FileTooLargeError(WorkspaceError):
    """Raised when a file exceeds a caller-supplied byte cap.

    Distinct from :class:`WorkspaceError` so callers (e.g. ``view_image``)
    can attach a use-case-specific remedy — a generic message cannot know
    whether the fix is downscaling an image or paging a log.
    """
