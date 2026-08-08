"""Protocols for workspace backends and sessions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Protocol

from .policy import Decision, WorkspacePolicy
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
)

if TYPE_CHECKING:
    from ..plugins.base import ViewInjector
    from ..tools import Tool

__all__ = ["ShellExecutor", "WorkspaceLike", "WorkspaceSession"]


class WorkspaceSession(Protocol):
    """Filesystem + process execution surface rooted at a workspace.

    Paths may be workspace-relative, absolute, or ``~``-prefixed; they are
    resolved (symlinks followed) and judged against the session's
    :class:`WorkspacePolicy` ACL on every operation, so custom tools that use
    the session directly are gated the same way the built-in tools are —
    ``deny`` raises, ``ask`` is the tool layer's job (``needs_approval``).
    """

    policy: WorkspacePolicy

    def decide_path(self, path: str, *, write: bool = False) -> Decision:
        """Policy decision for one path (for tool approval predicates)."""
        ...

    def decide_command(self, command: str, cwd: str = ".") -> Decision:
        """Combined decision for a shell command: static rules ⊕ path guard."""
        ...

    async def read_text(
        self, path: str, *, start: int | None = None, end: int | None = None
    ) -> FileContent:
        """Return UTF-8 text from ``path`` (optionally a 1-based line range)."""
        ...

    async def read_bytes(self, path: str, *, max_bytes: int | None = None) -> FileBytes:
        """Return the raw bytes of ``path`` (binary payloads, e.g. images).

        Raises :class:`~lovia.workspace.errors.FileTooLargeError` when the
        file exceeds ``max_bytes`` — sized *before* reading, so a caller's
        cap also bounds memory.
        """
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
        max_results: int | None = None,
    ) -> list[DirEntry]:
        """List entries under ``path``.

        Without ``pattern``, returns the direct children of ``path``. With a
        glob ``pattern`` (relative to ``path``), returns matching paths
        recursively per the pattern. ``max_results`` defaults to the session's
        configured limit; results past the cap are dropped (not an error).
        """
        ...

    async def grep(
        self,
        pattern: str,
        *,
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = False,
        include_hidden: bool = False,
        max_matches: int | None = None,
    ) -> list[GrepMatch]:
        """Search file contents under ``path`` with a regular expression.

        ``path`` may also be a single file. ``max_matches`` defaults to the
        session's configured limit; matches past the cap are dropped (not an
        error).
        """
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

    async def spawn(
        self,
        command: str,
        *,
        cwd: str = ".",
        env: Mapping[str, str] | None = None,
    ) -> ProcessStart:
        """Start a session-owned background process (same gate as ``run``).

        Returns immediately; output spools to a bounded buffer consumed via
        :meth:`read_process_output`. Background processes are ephemeral: they
        die with the session and are not restored by a checkpoint resume.
        """
        ...

    async def read_process_output(self, process_id: str) -> ProcessOutput:
        """Output since the last read plus running/exited status.

        Reading a known-but-exited process reports its outcome (never an
        error); an unknown id raises with the live ids in the message.
        """
        ...

    async def kill_process(self, process_id: str) -> ProcessOutput:
        """Kill a background process's whole group; report its final tail."""
        ...

    def background_processes(self) -> list[ProcessStatus]:
        """Passive status of every background process (consumes no output).

        Feeds the per-turn status reminder and any UI listing; returns
        ``[]`` when there are none (including after ``close()``).
        """
        ...

    async def close(self) -> None:
        """Release held resources (including live subprocesses). Idempotent."""
        ...


class ShellExecutor(Protocol):
    """Strategy for actually executing an approved shell command.

    The extension seam for OS-level enforcement: by default the session
    spawns commands as the host user; a sandboxing executor (macOS Seatbelt,
    Linux bubblewrap/Landlock, ...) can derive mount/permission scopes from
    the policy and make the path ACL *mandatory* for shell commands instead
    of advisory. Executors run *after* the policy/approval gates — they
    decide *how* a command runs, never *whether*. Output may be returned
    unclipped; the session applies its limits afterwards. An executor owns
    the processes it spawns, including killing them on cancellation.

    Executors cover one-shot ``run`` only: ``spawn`` (background processes)
    refuses to start under a custom executor rather than silently bypassing
    its sandbox.
    """

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float | None,
        policy: WorkspacePolicy,
        root: Path,
    ) -> CommandResult:
        """Execute ``command`` and return its captured result."""
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

    def view_injectors(self) -> list["ViewInjector"]:
        """Per-turn view injectors contributed by this workspace.

        Merged by the runner with the plugins' injectors (same transient
        per-call-view mechanism). The local backend contributes the
        background-process status reminder; return ``[]`` for none.
        """
        ...
