"""Local filesystem-backed sandbox implementation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from ..exceptions import UserError
from .errors import SandboxClosedError, SandboxError
from .paths import normalize_relative_path, resolve_existing, resolve_parent
from .protocol import SandboxBackend
from .types import CommandResult, DirEntry, FileChange, FileContent, SandboxSpec

__all__ = ["LocalSandboxBackend", "LocalSandboxSession"]


def _has_hidden_segment(rel: str) -> bool:
    return any(seg.startswith(".") for seg in rel.split("/") if seg)


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    return text[:limit], True


@dataclass(frozen=True)
class LocalSandboxBackend(SandboxBackend):
    """Backend that opens local filesystem sessions."""

    name: str = "local"

    async def open(self, spec: SandboxSpec) -> "LocalSandboxSession":
        return LocalSandboxSession(
            root=spec.root,
            env=spec.env,
            shell_timeout=spec.shell_timeout,
            max_read_chars=spec.max_read_chars,
            max_output_chars=spec.max_output_chars,
        )


@dataclass
class LocalSandboxSession:
    """A local sandbox session rooted at a host directory.

    This confines lovia file tools to ``root``. It is not a security boundary:
    approved shell commands run as the host user.
    """

    root: str | Path
    env: Mapping[str, str] | None = None
    shell_timeout: float | None = 300.0
    max_read_chars: int = 50_000
    max_output_chars: int = 50_000
    id: str = field(default_factory=lambda: f"local-{uuid.uuid4().hex[:8]}")
    _root: Path = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _locks: dict[str, asyncio.Lock] = field(
        default_factory=dict, init=False, repr=False
    )
    _locks_guard: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        if not root.is_dir():
            raise UserError(
                f"Sandbox root does not exist: {root}",
                hint="Point Sandbox.local(root=...) at an existing directory.",
            )
        self._root = root

    async def __aenter__(self) -> "LocalSandboxSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self._closed = True

    def _check_open(self) -> None:
        if self._closed:
            raise SandboxClosedError(f"Sandbox session {self.id} is closed.")

    async def _lock_for(self, rel: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(rel)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[rel] = lock
            return lock

    async def read_text(
        self, path: str, *, start: int | None = None, end: int | None = None
    ) -> FileContent:
        self._check_open()
        rel = normalize_relative_path(path)
        p = resolve_existing(self._root, rel)
        if not p.is_file():
            raise SandboxError(f"Not a file: {path}")
        if start is not None and start < 1:
            raise SandboxError("start must be >= 1.")
        if end is not None and end < 1:
            raise SandboxError("end must be >= 1.")
        if start is not None and end is not None and end < start:
            raise SandboxError("end must be >= start.")

        def _read() -> FileContent:
            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            total = len(lines)
            start_line = start or 1
            end_line = end or total
            selected = "".join(lines[start_line - 1 : end_line])
            content, truncated = _truncate_text(selected, self.max_read_chars)
            return FileContent(
                path=rel,
                content=content,
                start=start_line,
                end=min(end_line, total),
                total_lines=total,
                truncated=truncated or end_line < total,
            )

        return await asyncio.to_thread(_read)

    async def write_text(
        self, path: str, content: str, *, create_only: bool = False
    ) -> FileChange:
        self._check_open()
        rel = normalize_relative_path(path)
        lock = await self._lock_for(rel)
        async with lock:
            parent, name = resolve_parent(self._root, rel)
            p = parent / name

            def _write() -> FileChange:
                existed = p.exists()
                if existed and create_only:
                    return FileChange(
                        ok=False,
                        path=rel,
                        action="unchanged",
                        message="file already exists; retry without create_only to overwrite",
                    )
                parent.mkdir(parents=True, exist_ok=True)
                data = content.encode("utf-8")
                p.write_bytes(data)
                return FileChange(
                    path=rel,
                    action="updated" if existed else "created",
                    bytes_written=len(data),
                )

            return await asyncio.to_thread(_write)

    async def list_dir(
        self,
        path: str = ".",
        *,
        include_hidden: bool = False,
        max_results: int = 1_000,
    ) -> list[DirEntry]:
        self._check_open()
        rel = normalize_relative_path(path)
        p = resolve_existing(self._root, rel)
        if not p.is_dir():
            raise SandboxError(f"Not a directory: {path}")

        def _list() -> list[DirEntry]:
            entries: list[DirEntry] = []
            with os.scandir(p) as it:
                for entry in it:
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    if len(entries) >= max_results:
                        raise SandboxError(
                            f"Too many directory entries (> {max_results}).",
                            hint="Use a narrower path or increase max_results.",
                        )
                    try:
                        stat = entry.stat()
                        size = stat.st_size if entry.is_file() else None
                        mtime = stat.st_mtime
                    except OSError:
                        size, mtime = None, None
                    entries.append(
                        DirEntry(
                            name=entry.name,
                            is_dir=entry.is_dir(),
                            size=size,
                            mtime=mtime,
                        )
                    )
            entries.sort(key=lambda e: (not e.is_dir, e.name))
            return entries

        return await asyncio.to_thread(_list)

    async def glob(
        self,
        pattern: str,
        *,
        include_hidden: bool = False,
        max_results: int = 1_000,
    ) -> list[str]:
        self._check_open()
        rel_pattern = normalize_relative_path(pattern)

        def _glob() -> list[str]:
            results: list[str] = []
            for p in self._root.glob(rel_pattern):
                resolved = p.resolve()
                try:
                    resolved.relative_to(self._root)
                except ValueError:
                    continue
                rel = resolved.relative_to(self._root).as_posix()
                if not include_hidden and _has_hidden_segment(rel):
                    continue
                if len(results) >= max_results:
                    raise SandboxError(
                        f"Too many glob results (> {max_results}).",
                        hint="Use a narrower pattern or increase max_results.",
                    )
                results.append(rel)
            return sorted(set(results))

        return await asyncio.to_thread(_glob)

    async def run(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self._check_open()
        rel_cwd = normalize_relative_path(cwd)
        run_cwd = resolve_existing(self._root, rel_cwd)
        if not run_cwd.is_dir():
            raise SandboxError(f"Not a directory: {cwd}")
        merged_env = dict(os.environ)
        if self.env:
            merged_env.update(self.env)
        if env:
            merged_env.update(env)
        command_timeout = self.shell_timeout if timeout is None else timeout

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(run_cwd),
            env=merged_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=command_timeout
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
            return CommandResult(
                exit_code=None,
                stdout="",
                stderr=f"[timeout after {command_timeout}s]",
                timed_out=True,
            )

        stdout, stdout_truncated = _truncate_text(
            stdout_b.decode("utf-8", errors="replace"), self.max_output_chars
        )
        stderr, stderr_truncated = _truncate_text(
            stderr_b.decode("utf-8", errors="replace"), self.max_output_chars
        )
        return CommandResult(
            exit_code=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_truncated or stderr_truncated,
        )
