"""Sandbox exception hierarchy."""

from __future__ import annotations

from ..exceptions import ToolError

__all__ = [
    "PathOutsideSandboxError",
    "PermissionDeniedError",
    "SandboxBackendError",
    "SandboxCommandError",
    "SandboxClosedError",
    "SandboxError",
    "SandboxTimeoutError",
]


class SandboxError(ToolError):
    """Base class for sandbox-layer failures."""


class PathOutsideSandboxError(SandboxError):
    """Raised when a path escapes the sandbox root."""


class PermissionDeniedError(SandboxError):
    """Raised when the sandbox policy rejects an operation."""


class SandboxCommandError(SandboxError):
    """Raised for shell command setup failures."""


class SandboxTimeoutError(SandboxError):
    """Raised by callers that prefer exceptions over timed-out command results."""


class SandboxBackendError(SandboxError):
    """Raised when a sandbox backend cannot open or operate."""


class SandboxClosedError(SandboxError):
    """Raised when a closed session is used."""
