"""Sandbox layer for lovia.

Public surface (importable as ``lovia.sandbox.*``):

* Protocols: :class:`Sandbox`, :class:`SandboxProvider`
* Types: :class:`ExecResult`, :class:`ExecLimits`, :class:`DirEntry`,
  :class:`AuditVerdict`, :class:`AuditRecord`, :class:`AuditDecision`,
  :class:`AuditContext`, :class:`AuditStream`
* Errors: :class:`SandboxError`, :class:`PathEscape`,
  :class:`SandboxClosed`, :class:`AuditBlocked`
* Implementations: :class:`LocalSandbox`, :class:`LocalSandboxProvider`,
  :func:`single_sandbox_provider`
* Audit: :class:`AuditPolicy`, :class:`AuditToolPolicy`,
  :func:`default_audit_policy`, :func:`pass_through_policy`,
  :func:`compose_policies`, :func:`rule_policy`
* Wiring: :func:`attach_sandbox`, :func:`sandbox_tools`

A typical setup is one import line::

    from lovia.sandbox import LocalSandboxProvider, attach_sandbox
"""

from __future__ import annotations

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
    SandboxClosed,
    SandboxError,
)
from .local import LocalSandbox, LocalSandboxProvider, single_sandbox_provider
from .protocol import Sandbox, SandboxProvider
from .tools import sandbox_tools
from .types import AuditVerdict, DirEntry, ExecLimits, ExecResult
from .wire import attach_sandbox

__all__ = [
    # Protocols
    "Sandbox",
    "SandboxProvider",
    # Types
    "ExecResult",
    "ExecLimits",
    "DirEntry",
    "AuditVerdict",
    "AuditRecord",
    "AuditDecision",
    "AuditContext",
    "AuditStream",
    # Errors
    "SandboxError",
    "PathEscape",
    "SandboxClosed",
    "ExecTimeout",
    "AuditBlocked",
    # Local impl
    "LocalSandbox",
    "LocalSandboxProvider",
    "single_sandbox_provider",
    # Audit
    "AuditPolicy",
    "AuditToolPolicy",
    "default_audit_policy",
    "pass_through_policy",
    "compose_policies",
    "rule_policy",
    # Wiring
    "attach_sandbox",
    "sandbox_tools",
]
