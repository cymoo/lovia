"""Local filesystem-backed workspace session.

A :class:`LocalWorkspaceSession` is the single enforcement point for the
workspace policy's path ACL: every file operation resolves its path (symlinks
followed) and asks :meth:`~lovia.workspace.policy.WorkspacePolicy.decide_path`
— ``deny`` raises here, ``ask`` passes because the tool layer has already
routed it through the approval channel (the same split ``run`` uses for
command decisions). It is **not** an OS security boundary: an allowed shell
command runs as the host user. Hard isolation needs a sandboxed executor or
backend implementing the same protocols.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import stat as stat_module
import tempfile
import uuid
import weakref
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Mapping

from ..exceptions import UserError
from .command_guard import extract_path_claims
from .errors import PermissionDeniedError, WorkspaceClosedError, WorkspaceError
from .paths import ResolvedPath, resolve_path
from .policy import Decision, FileOp, WorkspacePolicy, merge_decisions

if TYPE_CHECKING:
    from .protocol import ShellExecutor
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
    # Optional OS-sandboxing strategy for shell commands; None spawns the
    # command directly as the host user. See protocol.ShellExecutor.
    executor: "ShellExecutor | None" = None
    id: str = field(default_factory=lambda: f"local-{uuid.uuid4().hex[:8]}")
    _root: Path = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    # Per-path write locks keyed by the *resolved* absolute path, so a symlink
    # and its target share one lock. Weak-valued so an entry disappears once no
    # in-flight operation references its lock — the map can't grow without
    # bound across a long-lived session.
    _locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = field(
        default_factory=weakref.WeakValueDictionary, init=False, repr=False
    )
    _locks_guard: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    # Live subprocesses, so close() (and only close()) can reap strays; each
    # run() also kills its own process on every exit path, including
    # cancellation.
    _procs: "set[asyncio.subprocess.Process]" = field(
        default_factory=set, init=False, repr=False
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
        procs, self._procs = list(self._procs), set()
        for proc in procs:
            _kill_process_group(proc)
        for proc in procs:
            with contextlib.suppress(Exception):
                await proc.wait()

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def decide_path(self, path: str, *, write: bool = False) -> Decision:
        """Policy decision for one path (used by tool approval predicates)."""
        rp = resolve_path(self._root, path)
        op: FileOp = "write" if write else "read"
        return self.policy.decide_path(rel=rp.rel, abs_posix=rp.abs_posix, op=op)

    def decide_command(self, command: str, cwd: str = ".") -> Decision:
        """Combined decision for a shell command: static rules ⊕ path guard.

        The static :meth:`WorkspacePolicy.decide_command` verdict is merged
        (most restrictive wins) with a path-ACL verdict for the working
        directory and for every path claim lexically extracted from the
        command (redirect targets as writes, path-looking arguments as
        reads). The extraction is advisory — see
        :mod:`lovia.workspace.command_guard` — but never *looser* than the
        static rules alone.
        """
        decision = self.policy.decide_command(command)
        if decision == "deny":
            return decision
        cwd_rp = resolve_path(self._root, cwd)
        decisions: list[Decision] = [
            decision,
            self.policy.decide_path(
                rel=cwd_rp.rel, abs_posix=cwd_rp.abs_posix, op="read"
            ),
        ]
        for op, token in extract_path_claims(command):
            rp = resolve_path(self._root, token, base=cwd_rp.abs)
            decisions.append(
                self.policy.decide_path(rel=rp.rel, abs_posix=rp.abs_posix, op=op)
            )
        return merge_decisions(*decisions)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _check_open(self) -> None:
        if self._closed:
            raise WorkspaceClosedError(f"Workspace session {self.id} is closed.")

    def _decide_or_raise(self, rp: ResolvedPath, op: FileOp) -> None:
        """Enforce the ACL: raise on ``deny``, pass ``allow`` and ``ask``.

        ``ask`` cannot be resolved here (approval is a runner concern); the
        tools gate it via ``needs_approval``, so by the time a call reaches
        the session it has either been approved or comes from custom code
        using the session directly — which is gated the same way ``run``
        handles command decisions.
        """
        decision = self.policy.decide_path(rel=rp.rel, abs_posix=rp.abs_posix, op=op)
        if decision != "deny":
            return
        verb = "Writing" if op == "write" else "Reading"
        if rp.rel is None:
            raise PermissionDeniedError(
                f"{verb} outside the workspace is not permitted: {rp.display()}",
                hint=(
                    "Access beyond the workspace root is controlled by policy. "
                    "The user can grant it via Workspace.local(readable=[...] / "
                    "writable=[...]); otherwise work within the workspace."
                ),
            )
        if (
            op == "write"
            and self.policy.decide_path(rel=rp.rel, abs_posix=rp.abs_posix, op="read")
            != "deny"
        ):
            # Reads pass but writes don't: a read-only workspace (or a
            # write-scoped rule), not a denied path.
            raise PermissionDeniedError(
                f"Workspace policy denies writing to {rp.display()!r}.",
                hint="Use mode='coding' (or a writable rule) to enable writes.",
            )
        raise PermissionDeniedError(
            f"Path {rp.display()!r} is denied by workspace policy.",
        )

    def _resolve_checked(self, path: str, *ops: FileOp) -> ResolvedPath:
        rp = resolve_path(self._root, path)
        for op in ops:
            self._decide_or_raise(rp, op)
        return rp

    def _display_and_rel(self, p: Path) -> tuple[str, str | None]:
        """(display path, workspace-relative path or None) for ``p``."""
        try:
            rel = p.relative_to(self._root).as_posix()
            return rel, rel
        except ValueError:
            return p.as_posix(), None

    def _read_denied(self, p: Path) -> bool:
        _, rel = self._display_and_rel(p)
        return (
            self.policy.decide_path(rel=rel, abs_posix=p.as_posix(), op="read")
            == "deny"
        )

    async def _lock_for(self, rp: ResolvedPath) -> asyncio.Lock:
        key = str(rp.abs)
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
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
        rp = self._resolve_checked(path, "read")
        p = rp.abs
        if not p.is_file():
            raise WorkspaceError(f"Not a file: {rp.display()}")
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
            # Bytes, not text mode: text mode's universal newlines would
            # silently normalize CRLF to LF, so the model would edit content
            # that does not match the file on disk.
            with p.open("rb") as fh:
                raw = fh.read(self.limits.max_file_read_bytes if oversized else -1)
            text = raw.decode("utf-8", errors="replace")
            if oversized:
                hint = (
                    "Large file: only its leading portion was read (line numbers "
                    "and total are for that portion). Use the shell, e.g. "
                    "sed -n, to read arbitrary ranges of very large files."
                )
            lines = text.splitlines(keepends=True)
            total = len(lines)
            start_line = start or 1
            end_line = end or total
            selected = "".join(lines[start_line - 1 : end_line])
            content, clipped = clip_text(
                selected, self.limits.max_file_read_chars, hint=hint
            )
            if oversized and selected and not clipped:
                # The byte cap cut the file mid-content, but the returned slice
                # still fit under the char cap, so clip_text added no notice.
                # Say so explicitly, or the model thinks it read the whole file.
                content = f"{content}\n[... {hint}]"
            return FileContent(
                path=rp.display(),
                content=content,
                start=start_line,
                end=min(end_line, total),
                total_lines=total,
                truncated=clipped or end_line < total or oversized,
            )

        # Take the per-path lock so a read never observes a half-written file
        # (write_text/edit_text hold the same lock).
        lock = await self._lock_for(rp)
        async with lock:
            return await asyncio.to_thread(_read)

    async def write_text(
        self, path: str, content: str, *, create_only: bool = False
    ) -> FileChange:
        self._check_open()
        rp = self._resolve_checked(path, "write")
        if rp.rel == ".":
            raise WorkspaceError(
                "Cannot write to the workspace root as a file.",
            )
        p = rp.abs
        lock = await self._lock_for(rp)
        async with lock:

            def _write() -> FileChange:
                if p.is_dir():
                    raise WorkspaceError(f"Is a directory: {rp.display()}")
                data = content.encode("utf-8")
                p.parent.mkdir(parents=True, exist_ok=True)
                if create_only:
                    # O_EXCL makes create-or-fail atomic (no exists() race).
                    try:
                        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                    except FileExistsError:
                        return FileChange(
                            ok=False,
                            path=rp.display(),
                            action="unchanged",
                            message=(
                                "file already exists; retry without create_only "
                                "to overwrite"
                            ),
                        )
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)
                    return FileChange(
                        path=rp.display(), action="created", bytes_written=len(data)
                    )
                existed = p.exists()
                _atomic_write(p, data)
                return FileChange(
                    path=rp.display(),
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
        # Editing both reads and writes the target, so it needs both sides of
        # the ACL (e.g. write_outside="ask" alone must not leak reads).
        rp = self._resolve_checked(path, "read", "write")
        p = rp.abs
        if not p.is_file():
            raise WorkspaceError(f"Not a file: {rp.display()}")
        lock = await self._lock_for(rp)
        async with lock:

            def _edit() -> EditResult:
                try:
                    if p.stat().st_size > self.limits.max_file_read_bytes:
                        return EditResult(
                            ok=False,
                            path=rp.display(),
                            message=(
                                "file is too large to edit safely; use the "
                                "shell for very large files"
                            ),
                        )
                except OSError:
                    pass
                # Bytes + strict decode: editing writes the file back, so a
                # lenient (errors="replace") read would persist U+FFFD and
                # destroy the original bytes. Refuse instead of corrupting.
                # Reading bytes (not text mode) also preserves CRLF line
                # endings instead of silently rewriting the whole file to LF.
                try:
                    text = p.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    return EditResult(
                        ok=False,
                        path=rp.display(),
                        message=(
                            "file is not valid UTF-8; cannot edit safely "
                            "(use the shell for binary/encoded files)"
                        ),
                    )
                old_text, new_text = old, new
                count = text.count(old_text)
                if count == 0 and "\r" not in old_text and "\r\n" in text:
                    # The model usually quotes the span with plain \n; if the
                    # file uses CRLF, retry with the CRLF form of the span (and
                    # of the replacement, so the file stays consistent).
                    crlf_old = old_text.replace("\n", "\r\n")
                    crlf_count = text.count(crlf_old)
                    if crlf_count:
                        old_text = crlf_old
                        new_text = new_text.replace("\n", "\r\n")
                        count = crlf_count
                if count == 0:
                    return EditResult(
                        ok=False,
                        path=rp.display(),
                        message=(
                            "old text not found; read the file again and retry "
                            "with the exact text (whitespace matters)"
                        ),
                    )
                if count > 1 and not replace_all:
                    return EditResult(
                        ok=False,
                        path=rp.display(),
                        replacements=count,
                        message=(
                            f"old text matched {count} times; include more "
                            "surrounding context to make it unique, or pass "
                            "replace_all=true to replace every occurrence"
                        ),
                    )
                if old_text == new_text:
                    return EditResult(
                        ok=True, path=rp.display(), replacements=count, changed=False
                    )
                updated = text.replace(old_text, new_text)
                _atomic_write(p, updated.encode("utf-8"))
                return EditResult(
                    ok=True, path=rp.display(), replacements=count, changed=True
                )

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
        rp = self._resolve_checked(path, "read")
        if not rp.abs.is_dir():
            raise WorkspaceError(f"Not a directory: {rp.display()}")
        if pattern is None:
            return await asyncio.to_thread(
                self._list_children, rp.abs, include_hidden, cap
            )
        return await asyncio.to_thread(
            self._list_matching, rp.abs, pattern, include_hidden, cap
        )

    def _entry_for(self, p: Path, *, is_dir: bool) -> DirEntry:
        display, _ = self._display_and_rel(p)
        symlink_target: str | None = None
        try:
            if p.is_symlink():
                symlink_target = p.resolve().as_posix()
        except OSError:
            symlink_target = None
        try:
            st = p.stat()
            size = None if is_dir else st.st_size
            mtime = st.st_mtime
        except OSError as exc:
            logger.debug("list_files: stat failed for %s (%s)", display, exc)
            size, mtime = None, None
        return DirEntry(
            path=display,
            is_dir=is_dir,
            size=size,
            mtime=mtime,
            symlink_target=symlink_target,
        )

    def _list_children(
        self, base: Path, include_hidden: bool, max_results: int
    ) -> ClippedList[DirEntry]:
        entries: list[DirEntry] = []
        truncated = False
        with os.scandir(base) as it:
            for entry in it:
                if not include_hidden and entry.name.startswith("."):
                    continue
                p = base / entry.name
                if self._read_denied(p):
                    continue
                # A symlink is also judged by where it leads, so a link to a
                # denied path is hidden just like the path itself would be.
                try:
                    if entry.is_symlink() and self._read_denied(p.resolve()):
                        continue
                except OSError:
                    pass
                if len(entries) >= max_results:
                    truncated = True
                    break
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                entries.append(self._entry_for(p, is_dir=is_dir))
        entries.sort(key=lambda e: (not e.is_dir, e.path))
        return ClippedList(entries, truncated=truncated)

    def _list_matching(
        self, base: Path, pattern: str, include_hidden: bool, max_results: int
    ) -> ClippedList[DirEntry]:
        if not pattern or pattern == "." or Path(pattern).is_absolute():
            raise WorkspaceError(f"Invalid glob pattern: {pattern!r}")
        if ".." in Path(pattern).parts:
            raise WorkspaceError(
                f"Invalid glob pattern: {pattern!r} ('..' is not supported; "
                "pass the directory as path= instead)"
            )
        results: dict[str, DirEntry] = {}
        truncated = False
        for p in base.glob(pattern):
            display, _ = self._display_and_rel(p)
            # Hidden filtering is relative to the *listed* directory: listing
            # ".config" explicitly must not hide everything just because the
            # base itself is a dotdir, and an outside base gets the same
            # treatment as an inside one.
            try:
                base_rel = p.relative_to(base).as_posix()
            except ValueError:  # defensive; glob yields paths under base
                base_rel = p.name
            if not include_hidden and _has_hidden_segment(base_rel):
                continue
            # Judge the resolved target (symlinks may point anywhere).
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if self._read_denied(p) or (resolved != p and self._read_denied(resolved)):
                continue
            if display in results:
                continue
            if len(results) >= max_results:
                truncated = True
                break
            try:
                is_dir = p.is_dir()
            except OSError:
                is_dir = False
            results[display] = self._entry_for(p, is_dir=is_dir)
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
        rp = self._resolve_checked(path, "read")
        base = rp.abs
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise WorkspaceError(f"Invalid regular expression: {exc}") from exc

        def _search() -> ClippedList[GrepMatch]:
            single_file = base.is_file()
            if single_file:
                files: Iterator[tuple[str, Path]] = iter([(rp.display(), base)])
            elif base.is_dir():
                files = self._walk_files(base, include_hidden)
            else:
                raise WorkspaceError(f"Not a file or directory: {rp.display()}")
            matches: list[GrepMatch] = []
            for file_display, file_path in files:
                # Filename-glob semantics: match the display path or its
                # basename — a filename *filter*, deliberately not the
                # gitignore-style *deny* matching used for policy patterns.
                if glob is not None and not (
                    fnmatch(file_display, glob)
                    or fnmatch(os.path.basename(file_display), glob)
                ):
                    continue
                # The walk only descends real directories under the target
                # (os.walk does not follow symlinked dirs), so only a
                # symlinked *file* can lead elsewhere: its target is inside
                # the already-permitted tree (fine), or it must be readable
                # on its own — "ask" is not resolvable mid-walk, so anything
                # short of "allow" is skipped rather than surfaced. A
                # single-file target was already gated as the operation's
                # subject, so it is exempt from this re-check.
                try:
                    if not single_file and file_path.is_symlink():
                        resolved = file_path.resolve()
                        if not resolved.is_relative_to(base):
                            _, res_rel = self._display_and_rel(resolved)
                            if (
                                self.policy.decide_path(
                                    rel=res_rel,
                                    abs_posix=resolved.as_posix(),
                                    op="read",
                                )
                                != "allow"
                            ):
                                continue
                except OSError as exc:
                    logger.debug("grep: resolve failed for %s (%s)", file_display, exc)
                    continue
                try:
                    if file_path.stat().st_size > self.limits.max_grep_file_bytes:
                        continue
                    with open(file_path, "rb") as fh:
                        head = fh.read(1024)
                        if b"\0" in head:
                            continue  # binary
                        data = head + fh.read()
                except OSError as exc:
                    logger.debug("grep: read failed for %s (%s)", file_display, exc)
                    continue
                text = data.decode("utf-8", errors="replace")
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        matches.append(
                            GrepMatch(
                                path=file_display,
                                line=line_no,
                                text=line.strip()[: self.limits.max_grep_line_chars],
                            )
                        )
                        if len(matches) >= cap:
                            return ClippedList(matches, truncated=True)
            return ClippedList(matches, truncated=False)

        return await asyncio.to_thread(_search)

    def _walk_files(
        self, base: Path, include_hidden: bool = False
    ) -> Iterator[tuple[str, Path]]:
        """Yield (display path, absolute path) for searchable files.

        Policy-denied paths are skipped (and dotfiles too unless
        ``include_hidden``). ``os.walk(followlinks=False)`` does not descend
        through symlinked directories, so the walk itself stays under
        ``base``; a symlinked *file* is still listed, so grep re-checks each
        one before reading.
        """
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dp = Path(dirpath)
            # In-place assignment (note the [:]) prunes os.walk's recursion:
            # hidden/denied subdirs are never descended into; the rest are
            # walked in sorted order. Rebinding dirnames would not prune.
            dirnames[:] = sorted(
                d
                for d in dirnames
                if (include_hidden or not d.startswith("."))
                and not self._read_denied(dp / d)
            )
            for name in sorted(filenames):
                if not include_hidden and name.startswith("."):
                    continue
                p = dp / name
                if self._read_denied(p):
                    continue
                display, _ = self._display_and_rel(p)
                yield display, p

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
        if self.decide_command(command, cwd) == "deny":
            raise PermissionDeniedError(
                "Command denied by workspace policy.",
                hint=(
                    "The command (or a path it references) is denied. Adjust "
                    "WorkspacePolicy rules or use a less restrictive mode."
                ),
            )
        cwd_rp = resolve_path(self._root, cwd)
        run_cwd = cwd_rp.abs
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

        if self.executor is not None:
            result = await self.executor.run(
                command,
                cwd=run_cwd,
                env=merged_env,
                timeout=command_timeout,
                policy=self.policy,
                root=self._root,
            )
            return self._clip_command_result(result)

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(run_cwd),
            env=merged_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._procs.add(proc)
        # close() may have run during the await above, after _check_open() and
        # before this registration — in which case it snapshotted an empty set
        # and this child would never be reaped. Re-check and self-reap so
        # close() stays best-effort even under that race.
        if self._closed:
            self._procs.discard(proc)
            _kill_process_group(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            raise WorkspaceClosedError(f"Workspace session {self.id} is closed.")

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=command_timeout
            )
        except asyncio.TimeoutError:
            _kill_process_group(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            return CommandResult(
                exit_code=None,
                stdout="",
                stderr=f"[timeout after {command_timeout}s]",
                timed_out=True,
            )
        except BaseException:
            # Cancellation (run aborted, tool timeout) or any other abrupt
            # exit must not orphan the child: kill its whole process group.
            _kill_process_group(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        finally:
            self._procs.discard(proc)

        return self._clip_command_result(
            CommandResult(
                exit_code=proc.returncode,
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
            )
        )

    def _clip_command_result(self, result: CommandResult) -> CommandResult:
        """Apply the session's output limits to a raw command result."""
        hint = "Re-run with a filter (e.g. pipe through grep/head) for more."
        stdout, stdout_clipped = clip_text(
            result.stdout,
            self.limits.max_shell_output_chars,
            hint=hint,
            keep_tail=True,
        )
        stderr, stderr_clipped = clip_text(
            result.stderr,
            self.limits.max_shell_output_chars,
            hint=hint,
            keep_tail=True,
        )
        if not stdout_clipped and not stderr_clipped:
            return result
        return result.model_copy(
            update={"stdout": stdout, "stderr": stderr, "truncated": True}
        )


def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """Kill ``proc`` and its process group (started with start_new_session)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


def _atomic_write(p: Path, data: bytes) -> None:
    """Write ``data`` to ``p`` via a same-directory temp file + rename.

    A crash mid-write can no longer truncate the target, and the original
    file mode survives the replacement.
    """
    mode: int | None = None
    with contextlib.suppress(OSError):
        mode = stat_module.S_IMODE(p.stat().st_mode)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode if mode is not None else 0o644)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
