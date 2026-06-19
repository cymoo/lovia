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
import logging
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
    ClippedList,
    CommandResult,
    DirEntry,
    EditResult,
    FileChange,
    FileContent,
    GrepMatch,
    WorkspaceLimits,
)
from ..tools.base import clip_text

__all__ = ["LocalWorkspaceSession"]

logger = logging.getLogger(__name__)

# Host env vars passed to shell commands by default: a minimal, non-secret
# base (a working PATH/locale) that deliberately excludes credentials —
# API keys, tokens — living in the parent process's environment. Widen with
# inherit_env=True, or pass specific vars via env=.
_ENV_PASSTHROUGH = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TZ", "TMPDIR"}
)


def _has_hidden_segment(rel: str) -> bool:
    return any(seg.startswith(".") for seg in rel.split("/") if seg)


@dataclass
class LocalWorkspaceSession:
    """A workspace session rooted at a local directory."""

    root: str | Path
    policy: WorkspacePolicy = field(default_factory=WorkspacePolicy)
    env: Mapping[str, str] | None = None
    shell_timeout: float | None = 300.0
    limits: WorkspaceLimits = field(default_factory=WorkspaceLimits)
    inherit_env: bool = False
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

    def _base_env(self) -> dict[str, str]:
        """Environment for shell commands.

        By default a minimal allowlist so commands get a working PATH/locale
        without inheriting the parent process's secrets (API keys, tokens).
        ``inherit_env=True`` passes the full host environment instead.
        """
        if self.inherit_env:
            return dict(os.environ)
        return {
            key: value
            for key, value in os.environ.items()
            if key in _ENV_PASSTHROUGH or key.startswith("LC_")
        }

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
            hint = "Use start/end to read the rest in pages."
            # Guard against loading a pathologically large file fully into
            # memory: read only a bounded prefix. Line ranges beyond that
            # prefix aren't reachable here — use the shell (e.g. sed -n) for
            # arbitrary ranges in very large files.
            try:
                oversized = p.stat().st_size > self.limits.max_file_read_bytes
            except OSError:
                oversized = False
            if oversized:
                with p.open("rb") as fh:
                    raw = fh.read(self.limits.max_file_read_bytes)
                text = raw.decode("utf-8", errors="replace")
            else:
                text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            total = len(lines)
            start_line = start or 1
            end_line = end or total
            selected = "".join(lines[start_line - 1 : end_line])
            content, clipped = clip_text(
                selected, self.limits.max_file_read_chars, hint=hint
            )
            return FileContent(
                path=rel,
                content=content,
                start=start_line,
                end=min(end_line, total),
                total_lines=total,
                truncated=clipped or end_line < total or oversized,
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
        max_results: int | None = None,
    ) -> list[DirEntry]:
        self._check_open()
        cap = self.limits.max_list_results if max_results is None else max_results
        rel, base = self._resolve_for_read(path)
        if not base.is_dir():
            raise WorkspaceError(f"Not a directory: {path}")
        if pattern is None:
            return await asyncio.to_thread(
                self._list_children, rel, base, include_hidden, cap
            )
        return await asyncio.to_thread(
            self._list_matching, base, pattern, include_hidden, cap
        )

    def _list_children(
        self, rel: str, base: Path, include_hidden: bool, max_results: int
    ) -> ClippedList[DirEntry]:
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
                except OSError as exc:
                    logger.debug("list_files: stat failed for %s (%s)", entry_rel, exc)
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
        return ClippedList(entries, truncated=truncated)

    def _list_matching(
        self, base: Path, pattern: str, include_hidden: bool, max_results: int
    ) -> ClippedList[DirEntry]:
        rel_pattern = normalize_relative_path(pattern)
        if rel_pattern == ".":
            raise WorkspaceError(f"Invalid glob pattern: {pattern!r}")
        results: dict[str, DirEntry] = {}
        truncated = False
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
                truncated = True
                break
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
        ordered = [results[key] for key in sorted(results)]
        return ClippedList(ordered, truncated=truncated)

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
        self._check_open()
        cap = self.limits.max_grep_matches if max_matches is None else max_matches
        rel, base = self._resolve_for_read(path)
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise WorkspaceError(f"Invalid regular expression: {exc}") from exc

        def _search() -> ClippedList[GrepMatch]:
            matches: list[GrepMatch] = []
            for file_rel, file_path in self._walk_files(rel, base, include_hidden):
                if glob is not None and not (
                    fnmatch(file_rel, glob) or fnmatch(os.path.basename(file_rel), glob)
                ):
                    continue
                # A symlinked file can point outside the root; grep must not
                # read through it (read_text already refuses such escapes).
                try:
                    if not file_path.resolve().is_relative_to(self._root):
                        continue
                except OSError:
                    continue
                try:
                    if file_path.stat().st_size > self.limits.max_grep_file_bytes:
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
                                text=line.strip()[: self.limits.max_grep_line_chars],
                            )
                        )
                        if len(matches) >= cap:
                            return ClippedList(matches, truncated=True)
            return ClippedList(matches, truncated=False)

        return await asyncio.to_thread(_search)

    def _walk_files(
        self, rel: str, base: Path, include_hidden: bool = False
    ) -> Iterator[tuple[str, Path]]:
        """Yield (workspace-relative path, absolute path) for searchable files.

        Policy-denied paths are skipped (and dotfiles too unless
        ``include_hidden``). Symlinked directories are not followed, so the
        walk itself cannot descend out of the root; callers still re-check
        individual symlinked files before reading them.
        """
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dir_rel = Path(dirpath).relative_to(self._root).as_posix()
            dirnames[:] = sorted(
                d
                for d in dirnames
                if (include_hidden or not d.startswith("."))
                and not self.policy.path_is_denied(
                    d if dir_rel == "." else f"{dir_rel}/{d}"
                )
            )
            for name in sorted(filenames):
                if not include_hidden and name.startswith("."):
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
        merged_env = self._base_env()
        # PWD should reflect the command's actual working directory, not the
        # host process's; set it before user overrides so env= can still win.
        merged_env["PWD"] = str(run_cwd)
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
            self.limits.max_shell_output_chars,
            hint=hint,
            keep_tail=True,
        )
        stderr, stderr_clipped = clip_text(
            stderr_b.decode("utf-8", errors="replace"),
            self.limits.max_shell_output_chars,
            hint=hint,
            keep_tail=True,
        )
        return CommandResult(
            exit_code=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_clipped or stderr_clipped,
        )
