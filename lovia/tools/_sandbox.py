"""Shared helpers for sandbox-backed tool factories."""

from __future__ import annotations

from ..sandbox.local import LocalSandboxSession
from ..sandbox.protocol import SandboxSession


def sandbox_session(
    *,
    root: str | None = None,
    session: SandboxSession | None = None,
) -> SandboxSession:
    if session is not None:
        return session
    return LocalSandboxSession(root=root or ".")
