"""The Files panel's change list and diff, read where the files are.

Two sources, merged by :func:`list_changes`: the paths this chat wrote (sent
by the page, each one ``stat``-ed — the cost follows the chat, not the tree)
and, in a git work tree, ``git status`` plus ``git diff --numstat HEAD``. Git
runs through the same readonly view session as every other panel read
(:func:`~lovia.web.workspaces.open_view` with ``git=True``), so a local,
SSH or Docker workspace answers alike, and every path git reports is held to
the view's read verdict before the page sees it.

Any failure to detect git — not installed, not a repository, a repository
git doesn't trust — reads as "not a git work tree": the list keeps the
chat's own files and the viewer has no diff.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import shlex
import subprocess
from dataclasses import dataclass

from ..workspace import (
    SCRATCH_DIR,
    PermissionDeniedError,
    WorkspaceDisconnectedError,
    WorkspaceError,
)
from ..workspace.protocol import WorkspaceSession
from ..workspace.types import CommandResult
from .schemas import ChangedFile, WorkspaceChanges, WorkspaceDiff

# `GIT_OPTIONAL_LOCKS=0`: the panel's reads never take the index lock from a
# git command the agent is running. `GIT_LITERAL_PATHSPECS=1`: a path is a
# path — never a glob or `:(top)` magic that reaches files it doesn't name.
_GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_LITERAL_PATHSPECS": "1"}
_GIT_TIMEOUT = 15.0
# Rows git alone contributes (the chat's own are never cut).
MAX_OTHERS = 200
_BINARY = re.compile(r"^(Binary files .* differ|GIT binary patch)$", re.M)


@dataclass(frozen=True)
class GitEntry:
    """One ``git status`` record, workspace-relative."""

    path: str
    status: str
    is_dir: bool = False
    old_path: str | None = None


async def _git(session: WorkspaceSession, args: str) -> CommandResult:
    return await session.run(f"git {args}", env=_GIT_ENV, timeout=_GIT_TIMEOUT)


async def git_prefix(session: WorkspaceSession) -> str | None:
    """The root's place in its repository (``""`` at the top, ``"sub/"``
    below it), or ``None`` when the root is not in a git work tree."""
    try:
        result = await _git(session, "rev-parse --show-prefix")
    except WorkspaceDisconnectedError:
        raise  # the host is gone: that is the answer, not "no git"
    except WorkspaceError:  # a view without git=True, an executor refusing
        return None
    return result.stdout.strip() if result.ok else None


def _under(path: str, prefix: str) -> str | None:
    """A repository-relative path as the workspace names it, or ``None``
    outside the workspace."""
    if not prefix:
        return path
    return path[len(prefix) :] if path.startswith(prefix) else None


def _status_of(xy: str) -> str:
    if xy == "??":
        return "untracked"
    if "D" in xy:
        return "deleted"
    if xy[0] in "RC":
        return "renamed"
    if xy[0] == "A":
        return "added"
    return "modified"


def parse_status(out: str, prefix: str) -> list[GitEntry]:
    """``git status --porcelain=v1 -z`` records (paths repository-relative)."""
    records = out.split("\0")
    entries: list[GitEntry] = []
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4 or rec[2] != " ":
            continue  # the trailing empty record
        xy, raw = rec[:2], rec[3:]
        old = None
        if xy[0] in "RC" and i < len(records):  # `XY new\0old\0`
            old = _under(records[i], prefix)
            i += 1
        path = _under(raw, prefix)
        if not path:
            continue
        is_dir = path.endswith("/")  # an untracked directory, folded
        entries.append(GitEntry(path.rstrip("/"), _status_of(xy), is_dir, old))
    return entries


def parse_numstat(out: str, prefix: str) -> dict[str, tuple[int, int]]:
    """``git diff --numstat -z`` as ``{path: (added, removed)}``; binary
    files (``-``) are left out."""
    records = out.split("\0")
    counts: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(records):
        added, _, rest = records[i].partition("\t")
        removed, _, path = rest.partition("\t")
        i += 1
        if not path and i + 1 < len(records):  # a rename: `a\tr\t\0old\0new`
            path = records[i + 1]
            i += 2
        rel = _under(path, prefix) if path else None
        if rel and added.isdigit() and removed.isdigit():
            counts[rel] = (int(added), int(removed))
    return counts


async def _readable(session: WorkspaceSession, path: str) -> bool:
    """Whether the view may read ``path`` without asking — what the panel
    shows is never more than the agent could read unasked."""
    verdict = session.decide_path(path)
    if inspect.isawaitable(verdict):  # a remote session asks its host
        verdict = await verdict
    return verdict.action == "allow"


def _is_scratch(path: str) -> bool:
    return path == SCRATCH_DIR or path.startswith(f"{SCRATCH_DIR}/")


async def _stat_touched(
    session: WorkspaceSession, paths: list[str]
) -> dict[str, ChangedFile]:
    """The chat's writes that are still there to show, by workspace path."""

    async def one(path: str) -> ChangedFile | None:
        try:
            if not await _readable(session, path):
                return None
            entry = await session.stat(path)
        except (OSError, PermissionDeniedError):
            return None  # gone, or never the panel's to show
        if entry.is_dir or _is_scratch(entry.path):
            return None
        return ChangedFile(
            path=entry.path,
            touched=True,
            size=entry.size,
            mtime=entry.mtime,
        )

    found = await asyncio.gather(*(one(p) for p in dict.fromkeys(paths)))
    return {f.path: f for f in found if f is not None}


async def list_changes(
    session: WorkspaceSession, touched_paths: list[str]
) -> WorkspaceChanges:
    """The change list: the chat's own files, newest first, then what else
    git sees uncommitted under the root."""
    touched = await _stat_touched(session, touched_paths)
    prefix = await git_prefix(session)
    if prefix is None:
        files = sorted(touched.values(), key=lambda f: f.mtime or 0.0, reverse=True)
        return WorkspaceChanges(git=False, files=files)

    status, numstat = await asyncio.gather(
        _git(session, "status --porcelain=v1 -z --untracked-files=normal -- ."),
        _git(session, "diff --numstat -z HEAD -- ."),
    )
    entries = parse_status(status.stdout, prefix) if status.ok else []
    # No HEAD yet (a repository without a commit): no counts, every file new.
    counts = parse_numstat(numstat.stdout, prefix) if numstat.ok else {}

    shown = await asyncio.gather(*(_readable(session, e.path) for e in entries))
    entries = [e for e, ok in zip(entries, shown) if ok and not _is_scratch(e.path)]
    by_path = {e.path: e for e in entries}
    new_dirs = [e.path + "/" for e in entries if e.is_dir]

    def with_git(f: ChangedFile, e: GitEntry | None) -> ChangedFile:
        added, removed = counts.get(f.path, (None, None))
        if e is None:  # inside an untracked directory, or unchanged
            inside = any(f.path.startswith(d) for d in new_dirs)
            return f.model_copy(update={"status": "untracked" if inside else None})
        return f.model_copy(
            update={
                "status": e.status,
                "old_path": e.old_path,
                "added": added,
                "removed": removed,
            }
        )

    mine = sorted(
        (with_git(f, by_path.get(f.path)) for f in touched.values()),
        key=lambda f: f.mtime or 0.0,
        reverse=True,
    )
    others = [
        with_git(ChangedFile(path=e.path, is_dir=e.is_dir), e)
        for e in sorted(entries, key=lambda e: e.path)
        if e.path not in touched
    ]
    truncated = len(others) > MAX_OTHERS or status.truncated
    return WorkspaceChanges(
        git=True, truncated=truncated, files=mine + others[:MAX_OTHERS]
    )


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut to whole lines within ``limit`` characters."""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    return text[: cut + 1 if cut > 0 else limit], True


async def file_diff(
    session: WorkspaceSession,
    path: str,
    *,
    limit: int,
    old: str | None = None,
    windows: bool = False,
) -> WorkspaceDiff | None:
    """``path`` against ``HEAD`` as unified text, or ``None`` outside a git
    work tree (or before its first commit). ``old`` is a rename's former
    path: with both named, git diffs the move instead of a new file.
    ``windows``: the command runs under cmd.exe, which knows no ``'``."""
    if await git_prefix(session) is None:
        return None
    quote = (lambda p: subprocess.list2cmdline([p])) if windows else shlex.quote
    paths = " ".join(quote(p) for p in (path, old) if p)
    result = await _git(session, f"diff -M --no-ext-diff --no-textconv HEAD -- {paths}")
    if not result.ok:
        return None
    if _BINARY.search(result.stdout):
        return WorkspaceDiff(path=path, diff="", binary=True)
    diff, truncated = _clip(result.stdout, limit)
    return WorkspaceDiff(path=path, diff=diff, truncated=truncated or result.truncated)
