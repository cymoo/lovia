"""Protocols for workspace backends and sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Protocol

from .policy import WorkspacePolicy
from .types import (
    CommandResult,
    DirEntry,
    EditResult,
    FileChange,
    FileContent,
    GrepMatch,
)

if TYPE_CHECKING:
    from ..tools import Tool

__all__ = ["WorkspaceLike", "WorkspaceSession"]


class WorkspaceSession(Protocol):
    """Filesystem + process execution surface rooted at a workspace.

    All paths are workspace-relative POSIX paths. Implementations enforce
    the session's :class:`WorkspacePolicy` path rules on every operation, so
    custom tools that use the session directly are gated the same way the
    built-in tools are.
    """

    policy: WorkspacePolicy

    async def read_text(
        self, path: str, *, start: int | None = None, end: int | None = None
    ) -> FileContent:
        """Return UTF-8 text from ``path`` (optionally a 1-based line range)."""
        ...

    async def write_text(
        self, path: str, content: str, *, create_only: bool = False
    ) -> FileChange:
        """Write UTF-8 text to ``path``, creating parent directories."""
        ...

    async def edit_text(
        self, path: str, old: str, new: str, *, replace_all: bool = False
    ) -> EditResult:
        """Atomically replace exact text in ``path``.

        Without ``replace_all`` the edit fails when ``old`` matches zero or
        multiple times; with it, every occurrence is replaced.
        """
        ...

    async def list_files(
        self,
        path: str = ".",
        *,
        pattern: str | None = None,
        include_hidden: bool = False,
        max_results: int = 500,
    ) -> list[DirEntry]:
        """List entries under ``path``.

        Without ``pattern``, returns the direct children of ``path``. With a
        glob ``pattern`` (relative to ``path``), returns matching paths
        recursively per the pattern.
        """
        ...

    async def grep(
        self,
        pattern: str,
        *,
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = False,
        max_matches: int = 100,
    ) -> list[GrepMatch]:
        """Search file contents under ``path`` with a regular expression."""
        ...

    async def run(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run a one-shot, non-interactive shell command."""
        ...

    async def close(self) -> None:
        """Release held resources. Idempotent."""
        ...


class WorkspaceLike(Protocol):
    """Configuration object accepted by ``Agent.workspace``."""

    # Read-only so frozen-dataclass configs (e.g. ``Workspace``) satisfy the
    # protocol. A plain ``close_after_run: bool`` would demand a *settable*
    # attribute, which a ``@dataclass(frozen=True)`` field is not.
    @property
    def close_after_run(self) -> bool:
        """Whether the runner should close sessions it opened for a run."""
        ...

    async def open(self) -> WorkspaceSession:
        """Open a workspace session."""
        ...

    def tools(self) -> list["Tool"]:
        """Return the built-in tool bundle permitted by this workspace."""
        ...

    def instructions(self) -> str:
        """Return the workspace prompt fragment for the system prompt."""
        ...
