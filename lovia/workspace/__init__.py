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
    FileTooLargeError,
    PermissionDeniedError,
    WorkspaceClosedError,
    WorkspaceError,
)
from .local import LocalWorkspaceSession
from .policy import (
    CommandRule,
    Decision,
    FileOp,
    PathRule,
    Verdict,
    WorkspacePolicy,
    merge_verdicts,
)

# ``WorkspaceLike`` / ``WorkspaceSession`` / ``ShellExecutor`` are the
# extension protocols for authoring a custom backend or sandboxing executor;
# they stay importable from here (and from ``lovia.workspace.protocol``) but
# are kept out of ``__all__`` so the advertised surface is the handful of
# types day-to-day users actually touch.
from .protocol import ShellExecutor, WorkspaceLike, WorkspaceSession  # noqa: F401
from .rules import (
    CREDENTIAL_PATHS,
    DANGEROUS_COMMANDS,
    DEFAULT_BASELINE,
    DEV_TOOLS,
    IRREVERSIBLE,
    PROTECTED_CONFIG,
    READ_ONLY,
)
from .types import (
    CommandResult,
    DirEntry,
    EditResult,
    FileBytes,
    FileChange,
    FileContent,
    GrepMatch,
    ProcessOutput,
    ProcessStart,
    ProcessStatus,
    WorkspaceLimits,
    WorkspaceMode,
)

# ``WorkspaceSessionBinding`` is the return type of ``LocalWorkspace.bind`` /
# ``.session()`` — importable for typing, but out of ``__all__`` like the
# protocols above (callers construct it via those methods, never directly).
from .workspace import (  # noqa: F401
    SCRATCH_DIR,
    LocalWorkspace,
    Workspace,
    WorkspaceSessionBinding,
)

__all__ = [
    "CREDENTIAL_PATHS",
    "CommandResult",
    "CommandRule",
    "DANGEROUS_COMMANDS",
    "DEFAULT_BASELINE",
    "DEV_TOOLS",
    "Decision",
    "DirEntry",
    "EditResult",
    "FileBytes",
    "FileChange",
    "FileContent",
    "FileOp",
    "FileTooLargeError",
    "GrepMatch",
    "IRREVERSIBLE",
    "LocalWorkspace",
    "LocalWorkspaceSession",
    "PROTECTED_CONFIG",
    "PathRule",
    "PermissionDeniedError",
    "READ_ONLY",
    "ProcessOutput",
    "ProcessStart",
    "ProcessStatus",
    "SCRATCH_DIR",
    "Verdict",
    "Workspace",
    "WorkspaceClosedError",
    "WorkspaceError",
    "WorkspaceLimits",
    "WorkspaceMode",
    "WorkspacePolicy",
    "merge_verdicts",
]
