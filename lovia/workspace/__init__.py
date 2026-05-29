"""Workspace tools for local code agents.

``lovia.workspace`` is opt-in and intentionally separate from ``import lovia``.
The default local workspace is convenient for coding agents, but it is not a
security boundary: file writes affect real files and commands run as the host
user.
"""

from .audit import (
    AuditContext,
    AuditDecision,
    AuditPolicy,
    AuditRecord,
    AuditStream,
    AuditToolPolicy,
    compose_policies,
    default_audit_policy,
    pass_through_policy,
    rule_policy,
)
from .errors import (
    AuditBlocked,
    ExecTimeout,
    PathEscape,
    WorkspaceClosed,
    WorkspaceError,
)
from .local import LocalWorkspace
from .protocol import WorkspaceBackend
from .tools import bash, edit_file, glob, list_dir, read_file, write_file
from .types import AuditVerdict, DirEntry, ExecLimits, ExecResult
from .workspace import Workspace, default_workspace

__all__ = [
    "AuditBlocked",
    "AuditContext",
    "AuditDecision",
    "AuditPolicy",
    "AuditRecord",
    "AuditStream",
    "AuditToolPolicy",
    "AuditVerdict",
    "DirEntry",
    "ExecLimits",
    "ExecResult",
    "ExecTimeout",
    "LocalWorkspace",
    "PathEscape",
    "Workspace",
    "WorkspaceBackend",
    "WorkspaceClosed",
    "WorkspaceError",
    "bash",
    "compose_policies",
    "default_audit_policy",
    "default_workspace",
    "edit_file",
    "glob",
    "list_dir",
    "pass_through_policy",
    "read_file",
    "rule_policy",
    "write_file",
]
