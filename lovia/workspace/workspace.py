"""User-facing workspace wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Mapping, Sequence

from .local import LocalWorkspace
from .protocol import WorkspaceBackend
from .types import DirEntry, ExecLimits, ExecResult

__all__ = ["Workspace", "default_workspace"]


@dataclass
class Workspace:
    """A filesystem/process boundary for code tools.

    The default local workspace points at a real host directory. It confines
    lovia file operations to ``root``, but it is not a security boundary:
    commands run as the host user and writes modify real files.
    """

    root: str | Path = "."
    backend: WorkspaceBackend | None = None
    workspace: str = "/workspace"
    max_bytes: int = 1_000_000
    env: Mapping[str, str] | None = None
    create: bool = False
    ephemeral: bool = False
    env_isolation: bool = False
    adaptive_python: bool = True
    _backend: WorkspaceBackend = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.backend is None:
            self._backend = LocalWorkspace(
                root=self.root,
                workspace=self.workspace,
                max_bytes=self.max_bytes,
                env=self.env,
                create=self.create,
                ephemeral=self.ephemeral,
                env_isolation=self.env_isolation,
                adaptive_python=self.adaptive_python,
            )
        else:
            self._backend = self.backend

    @property
    def id(self) -> str:
        return self._backend.id

    async def read_file(
        self,
        path: str,
        *,
        start_line: int | None = None,
        max_lines: int | None = None,
        max_bytes: int | None = None,
    ) -> str:
        """Read a UTF-8 file as raw text, optionally by line range."""

        data = await self._backend.read(path, max_bytes=max_bytes)
        text = data.decode("utf-8", errors="replace")
        if start_line is None and max_lines is None:
            return text
        if start_line is not None and start_line < 1:
            raise ValueError("start_line must be >= 1")
        lines = text.splitlines(keepends=True)
        start = (start_line - 1) if start_line is not None else 0
        end = (start + max_lines) if max_lines is not None else None
        return "".join(lines[start:end])

    async def write_file(
        self,
        path: str,
        content: str,
        *,
        append: bool = False,
        overwrite: bool = True,
    ) -> int:
        """Write UTF-8 text and return bytes written."""

        return await self._backend.write(
            path, content, append=append, overwrite=overwrite
        )

    async def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
    ) -> int:
        """Replace exact text in a file and return replacement count."""

        return await self._backend.edit(
            path, old_text, new_text, replace_all=replace_all
        )

    async def list_dir(
        self,
        path: str = ".",
        *,
        include_hidden: bool = False,
        max_results: int = 1_000,
    ) -> list[DirEntry]:
        """List direct children of a directory."""

        return await self._backend.list_dir(
            path, include_hidden=include_hidden, max_results=max_results
        )

    async def glob(
        self,
        pattern: str,
        *,
        include_hidden: bool = False,
        max_results: int = 1_000,
    ) -> list[str]:
        """Return sorted workspace-relative paths matching ``pattern``."""

        return await self._backend.glob(
            pattern, include_hidden=include_hidden, max_results=max_results
        )

    async def exists(self, path: str) -> bool:
        return await self._backend.exists(path)

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        await self._backend.remove(path, recursive=recursive)

    async def run(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | bytes | None = None,
        limits: ExecLimits | None = None,
    ) -> ExecResult:
        """Run a command inside the workspace root."""

        return await self._backend.exec(
            command, cwd=cwd, env=env, stdin=stdin, limits=limits
        )

    async def close(self) -> None:
        await self._backend.close()


_default_lock = RLock()
_default_workspaces: dict[Path, Workspace] = {}


def default_workspace(root: str | Path | None = None) -> Workspace:
    """Return the cwd-keyed default workspace."""

    key = Path(root or ".").expanduser().resolve()
    with _default_lock:
        ws = _default_workspaces.get(key)
        if ws is None:
            ws = Workspace(root=key)
            _default_workspaces[key] = ws
        return ws
