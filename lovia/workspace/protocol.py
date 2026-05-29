"""Protocols for workspace backends."""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence, runtime_checkable

from .types import DirEntry, ExecLimits, ExecResult

__all__ = ["WorkspaceBackend"]


@runtime_checkable
class WorkspaceBackend(Protocol):
    """Filesystem + process execution surface rooted at a workspace."""

    id: str
    workspace: str

    async def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        """Return bytes from ``path``."""
        ...

    async def write(
        self,
        path: str,
        data: bytes | str,
        *,
        append: bool = False,
        overwrite: bool = True,
    ) -> int:
        """Write bytes or text to ``path`` and return bytes written."""
        ...

    async def edit(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
    ) -> int:
        """Replace exact text in a UTF-8 file and return replacement count."""
        ...

    async def list_dir(
        self,
        path: str = ".",
        *,
        include_hidden: bool = False,
        max_results: int = 1_000,
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
        """Return sorted workspace-relative paths matching ``pattern``."""
        ...

    async def exists(self, path: str) -> bool:
        """Return True if ``path`` exists inside the workspace."""
        ...

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """Remove ``path``."""
        ...

    async def exec(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | bytes | None = None,
        limits: ExecLimits | None = None,
    ) -> ExecResult:
        """Run ``command`` and return the result."""
        ...

    async def close(self) -> None:
        """Release any held resources. Idempotent."""
        ...
