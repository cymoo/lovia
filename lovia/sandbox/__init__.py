"""Sandbox backends and sessions for filesystem/process tools."""

from __future__ import annotations

from .errors import (
    PathOutsideSandboxError,
    PermissionDeniedError,
    SandboxBackendError,
    SandboxClosedError,
    SandboxCommandError,
    SandboxError,
    SandboxTimeoutError,
)
from .local import LocalSandboxBackend, LocalSandboxSession
from .protocol import SandboxBackend, SandboxLike, SandboxSession
from .sandbox import Sandbox
from .types import (
    CommandResult,
    DirEntry,
    EditResult,
    FileChange,
    FileContent,
    SandboxMode,
    SandboxSpec,
)

__all__ = [
    "CommandResult",
    "DirEntry",
    "EditResult",
    "FileChange",
    "FileContent",
    "LocalSandboxBackend",
    "LocalSandboxSession",
    "PathOutsideSandboxError",
    "PermissionDeniedError",
    "Sandbox",
    "SandboxBackend",
    "SandboxBackendError",
    "SandboxClosedError",
    "SandboxCommandError",
    "SandboxError",
    "SandboxLike",
    "SandboxMode",
    "SandboxSession",
    "SandboxSpec",
    "SandboxTimeoutError",
]
