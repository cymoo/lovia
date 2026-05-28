"""Exception hierarchy for the sandbox layer.

All sandbox errors derive from :class:`SandboxError`, which itself derives
from :class:`~lovia.exceptions.ToolError` so existing error-handling that
catches the framework's ``ToolError`` continues to catch sandbox failures
unmodified.
"""

from __future__ import annotations

from ..exceptions import ToolError

__all__ = [
    "AuditBlocked",
    "ExecTimeout",
    "PathEscape",
    "SandboxClosed",
    "SandboxError",
]


class SandboxError(ToolError):
    """Base class for sandbox-layer failures."""


class PathEscape(SandboxError):
    """Raised when a path resolves outside the sandbox root."""


class SandboxClosed(SandboxError):
    """Raised when a method is invoked on a closed sandbox."""


class ExecTimeout(SandboxError):
    """Raised by callers that prefer an exception over a timed-out
    :class:`ExecResult`. The default ``exec`` returns a result; only
    surface this when explicitly opted in."""


class AuditBlocked(SandboxError):
    """Raised when :class:`AuditPolicy` blocks a command."""
