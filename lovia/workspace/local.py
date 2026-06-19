"""Local filesystem-backed workspace session.

A :class:`LocalWorkspaceSession` confines lovia's file tools to a host
directory and enforces the workspace policy's path rules on every
operation. It is **not** an OS security boundary: shell commands the policy
allows (or a human approves) run as the host user. Hard isolation needs a
sandboxed backend (e.g. a container) implementing the same
:class:`~lovia.workspace.protocol.WorkspaceSession` protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterator, Mapping

from ..exceptions import UserError
from .errors import PermissionDeniedError, WorkspaceClosedError, WorkspaceError
from .paths import normalize_relative_path, resolve_existing, resolve_parent
from .policy import WorkspacePolicy
from .types import (
    CommandResult,
    DirEntry,
    EditResult,
    FileChange,
    FileContent,
    GrepMatch,
    clip_text,
)

__all__ = ["LocalWorkspaceSession"]

# Files larger than this are skipped by grep (binary blobs, build artifacts).
_GREP_MAX_FILE_BYTES = 5_000_000
# Matched lines are clipped so one minified file can't flood the result.
_GREP_MAX_LINE_CHARS = 400


def _has_hidden_segment(rel: str) -> bool:
    return any(seg.startswith(".") for seg in rel.split("/") if seg)


@dataclass
class LocalWorkspaceSession:
    """A workspace session rooted at a local directory."""

    root: str | Path
    policy: WorkspacePolicy = field(default_factory=WorkspacePolicy)
    env: Mapping[str, str] | None = None
    shell_timeout: float | None = 300.0
    max_read_chars: int = 50_000
    max_output_chars: int = 30_000
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
                f"Workspace root does not exist: {root}",
                hint="Point Workspace.local(root=...) at an existing directory.",
            )
        self._root = root

    async def __aenter__(self) -> "LocalWorkspaceSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _check_open(self) -> None:
        if self._closed:
            raise WorkspaceClosedError(f"Workspace session {self.id} is closed.")

    def _resolve_for_read(self, path: str) -> tuple[str, Path]:
        rel = normalize_relative_path(path)
        self.policy.check_path(rel, write=False)
        return rel, resolve_existing(self._root, rel)

    def _resolve_for_write(self, path: str) -> tuple[str, Path]:
        rel = normalize_relative_path(path)
        self.policy.check_path(rel, write=True)
        parent, name = resolve_parent(self._root, rel)
        return rel, parent / name

    async def _lock_for(self, rel: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(rel)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[rel] = lock
            return lock

    # ------------------------------------------------------------------ #
    # Files
    # ------------------------------------------------------------------ #

    async def read_text(
        self, path: str, *, start: int | None = None, end: int | None = None
    ) -> FileContent:
        self._check_open()
        rel, p = self._resolve_for_read(path)
        if not p.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        if start is not None and start < 1:
            raise WorkspaceError("start must be >= 1.")
        if end is not None and end < 1:
            raise WorkspaceError("end must be >= 1.")
        if start is not None and end is not None and end < start:
            raise WorkspaceError("end must be >= start.")

        def _read() -> FileContent:
            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            total = len(lines)
            start_line = start or 1
            end_line = end or total
            selected = "".join(lines[start_line - 1 : end_line])
            content, clipped = clip_text(
                selected,
                self.max_read_chars,
                hint="Use start/end to read the rest in pages.",
            )
            return FileContent(
                path=rel,
                content=content,
                start=start_line,
                end=min(end_line, total),
                total_lines=total,
                truncated=clipped or end_line < total,
            )

        return await asyncio.to_thread(_read)

    async def write_text(
        self, path: str, content: str, *, create_only: bool = False
    ) -> FileChange:
        self._check_open()
        rel, p = self._resolve_for_write(path)
        lock = await self._lock_for(rel)
        async with lock:

            def _write() -> FileChange:
                existed = p.exists()
                if existed and create_only:
                    return FileChange(
                        ok=False,
                        path=rel,
                        action="unchanged",
                        message="file already exists; retry without create_only to overwrite",
                    )
                p.parent.mkdir(parents=True, exist_ok=True)
                data = content.encode("utf-8")
                p.write_bytes(data)
                return FileChange(
                    path=rel,
                    action="updated" if existed else "created",
                    bytes_written=len(data),
                )

            return await asyncio.to_thread(_write)

    async def edit_text(
        self, path: str, old: str, new: str, *, replace_all: bool = False
    ) -> EditResult:
        """Atomic read-modify-write under the per-path lock."""
        self._check_open()
        if old == "":
            return EditResult(
                ok=False,
                path=path,
                message="old must not be empty; read the file and provide an exact span",
            )
        rel, p = self._resolve_for_write(path)
        if not p.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        lock = await self._lock_for(rel)
        async with lock:

            def _edit() -> EditResult:
                text = p.read_text(encoding="utf-8", errors="replace")
                count = text.count(old)
                if count == 0:
                    return EditResult(
                        ok=False,
                        path=rel,
                        message=(
                            "old text not found; read the file again and retry "
                            "with the exact text (whitespace matters)"
                        ),
                    )
                if count > 1 and not replace_all:
                    return EditResult(
                        ok=False,
                        path=rel,
                        replacements=count,
                        message=(
                            f"old text matched {count} times; include more "
                            "surrounding context to make it unique, or pass "
                            "replace_all=true to replace every occurrence"
                        ),
                    )
                if old == new:
                    return EditResult(
                        ok=True, path=rel, replacements=count, changed=False
                    )
                updated = text.replace(old, new)
                p.write_text(updated, encoding="utf-8")
                return EditResult(ok=True, path=rel, replacements=count, changed=True)

            return await asyncio.to_thread(_edit)

    # ------------------------------------------------------------------ #
    # Listing & search
    # ------------------------------------------------------------------ #

    async def list_files(
        self,
        path: str = ".",
        *,
        pattern: str | None = None,
        include_hidden: bool = False,
        max_results: int = 500,
    ) -> list[DirEntry]:
        self._check_open()
        rel, base = self._resolve_for_read(path)
        if not base.is_dir():
            raise WorkspaceError(f"Not a directory: {path}")
        if pattern is None:
            return await asyncio.to_thread(
                self._list_children, rel, base, include_hidden, max_results
            )
        return await asyncio.to_thread(
            self._list_matching, base, pattern, include_hidden, max_results
        )

    def _list_children(
        self, rel: str, base: Path, include_hidden: bool, max_results: int
    ) -> list[DirEntry]:
        entries: list[DirEntry] = []
        truncated = False
        with os.scandir(base) as it:
            for entry in it:
                if not include_hidden and entry.name.startswith("."):
                    continue
                entry_rel = entry.name if rel == "." else f"{rel}/{entry.name}"
                if self.policy.path_is_denied(entry_rel):
                    continue
                if len(entries) >= max_results:
                    truncated = True
                    break
                try:
                    stat = entry.stat()
                    size = stat.st_size if entry.is_file() else None
                    mtime = stat.st_mtime
                except OSError:
                    # TODO: 是否应该log，或更好的处理方式？
                    size, mtime = None, None
                entries.append(
                    DirEntry(
                        path=entry_rel,
                        is_dir=entry.is_dir(),
                        size=size,
                        mtime=mtime,
                    )
                )
        entries.sort(key=lambda e: (not e.is_dir, e.path))
        # TODO: 这个应该先判断吧
        if truncated:
            raise WorkspaceError(
                f"Too many directory entries (> {max_results}).",
                hint="List a narrower path or raise max_results.",
            )
        return entries

    def _list_matching(
        self, base: Path, pattern: str, include_hidden: bool, max_results: int
    ) -> list[DirEntry]:
        rel_pattern = normalize_relative_path(pattern)
        if rel_pattern == ".":
            raise WorkspaceError(f"Invalid glob pattern: {pattern!r}")
        results: dict[str, DirEntry] = {}
        for p in base.glob(rel_pattern):
            resolved = p.resolve()
            try:
                entry_rel = resolved.relative_to(self._root).as_posix()
            except ValueError:
                continue  # symlink escaping the root
            if not include_hidden and _has_hidden_segment(entry_rel):
                continue
            if self.policy.path_is_denied(entry_rel):
                continue
            if len(results) >= max_results:
                raise WorkspaceError(
                    f"Too many matches (> {max_results}).",
                    hint="Use a narrower pattern or raise max_results.",
                )
            try:
                stat = resolved.stat()
                is_dir = resolved.is_dir()
                size = None if is_dir else stat.st_size
                mtime = stat.st_mtime
            except OSError:
                is_dir, size, mtime = False, None, None
            results[entry_rel] = DirEntry(
                path=entry_rel, is_dir=is_dir, size=size, mtime=mtime
            )
        return [results[key] for key in sorted(results)]

    async def grep(
        self,
        pattern: str,
        *,
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = False,
        max_matches: int = 100,
    ) -> list[GrepMatch]:
        self._check_open()
        rel, base = self._resolve_for_read(path)
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise WorkspaceError(f"Invalid regular expression: {exc}") from exc

        def _search() -> list[GrepMatch]:
            matches: list[GrepMatch] = []
            for file_rel, file_path in self._walk_files(rel, base):
                if glob is not None and not (
                    fnmatch(file_rel, glob) or fnmatch(os.path.basename(file_rel), glob)
                ):
                    continue
                try:
                    if file_path.stat().st_size > _GREP_MAX_FILE_BYTES:
                        continue
                    with open(file_path, "rb") as fh:
                        head = fh.read(1024)
                        if b"\0" in head:
                            continue  # binary
                        data = head + fh.read()
                except OSError:
                    continue
                text = data.decode("utf-8", errors="replace")
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        matches.append(
                            GrepMatch(
                                path=file_rel,
                                line=line_no,
                                text=line.strip()[:_GREP_MAX_LINE_CHARS],
                            )
                        )
                        if len(matches) >= max_matches:
                            return matches
            return matches

        return await asyncio.to_thread(_search)

    def _walk_files(self, rel: str, base: Path) -> Iterator[tuple[str, Path]]:
        """Yield (workspace-relative path, absolute path) for searchable files.

        Hidden and policy-denied paths are skipped. Symlinked directories are
        not followed, so the walk cannot escape the root.
        """
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dir_rel = Path(dirpath).relative_to(self._root).as_posix()
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not d.startswith(".")
                and not self.policy.path_is_denied(
                    d if dir_rel == "." else f"{dir_rel}/{d}"
                )
            )
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                file_rel = name if dir_rel == "." else f"{dir_rel}/{name}"
                if self.policy.path_is_denied(file_rel):
                    continue
                yield file_rel, Path(dirpath) / name

    # ------------------------------------------------------------------ #
    # Shell
    # ------------------------------------------------------------------ #

    async def run(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self._check_open()
        # Defense in depth: a policy-denied command is refused even when a
        # custom tool calls the session directly. "ask" cannot be resolved
        # here (approval is a runner concern); the shell tool gates it.
        if self.policy.decide_command(command) == "deny":
            raise PermissionDeniedError(
                "Command denied by workspace policy.",
                hint="Adjust WorkspacePolicy.command_rules or use a less restrictive mode.",
            )
        rel_cwd = normalize_relative_path(cwd)
        run_cwd = resolve_existing(self._root, rel_cwd)
        if not run_cwd.is_dir():
            raise WorkspaceError(f"Not a directory: {cwd}")
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

        hint = "Re-run with a filter (e.g. pipe through grep/head) for more."
        stdout, stdout_clipped = clip_text(
            stdout_b.decode("utf-8", errors="replace"),
            self.max_output_chars,
            hint=hint,
            keep_tail=True,
        )
        stderr, stderr_clipped = clip_text(
            stderr_b.decode("utf-8", errors="replace"),
            self.max_output_chars,
            hint=hint,
            keep_tail=True,
        )
        return CommandResult(
            exit_code=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_clipped or stderr_clipped,
        )
