"""Protocols for sandbox backends and sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Protocol, runtime_checkable

from .types import CommandResult, DirEntry, FileChange, FileContent, SandboxSpec

if TYPE_CHECKING:
    from ..tools import Tool

__all__ = ["SandboxBackend", "SandboxLike", "SandboxSession"]


@runtime_checkable
class SandboxSession(Protocol):
    """Filesystem + process execution surface rooted at a sandbox."""

    async def read_text(
        self, path: str, *, start: int | None = None, end: int | None = None
    ) -> FileContent:
        """Return UTF-8 text from ``path``."""
        ...

    async def write_text(
        self, path: str, content: str, *, create_only: bool = False
    ) -> FileChange:
        """Write UTF-8 text to ``path``."""
        ...

    async def list_dir(
        self, path: str = ".", *, include_hidden: bool = False, max_results: int = 1_000
    ) -> list[DirEntry]:
        """List entries directly under ``path``."""
        ...

    async def glob(
        self,
        pattern: str,
        *,
        include_hidden: bool = False,
        max_results: int = 1_000,
    ) -> list[str]:
        """Return sorted sandbox-relative paths matching ``pattern``."""
        ...

    async def run(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run ``command`` and return the result."""
        ...

    async def close(self) -> None:
        """Release held resources. Idempotent."""
        ...


@runtime_checkable
class SandboxBackend(Protocol):
    """Factory for sandbox sessions."""

    name: str

    async def open(self, spec: SandboxSpec) -> SandboxSession:
        """Open a sandbox session for ``spec``."""
        ...


@runtime_checkable
class SandboxLike(Protocol):
    """Configuration object that can open a sandbox session."""

    mode: str
    close_on_run: bool

    async def open(self) -> SandboxSession:
        """Open a sandbox session."""
        ...

    def tools(self, session: SandboxSession) -> list["Tool"]:
        """Build tool objects bound to ``session``."""
        ...

    def instructions(self) -> str:
        """Return the sandbox prompt fragment."""
        ...
