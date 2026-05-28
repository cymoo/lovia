"""Workspace path utilities shared by every Sandbox implementation.

A *workspace path* is a POSIX-style string the model sees. It is either:

* an absolute path under the logical workspace root (``workspace="/workspace"``);
* a relative path interpreted from that workspace root.

Both are mapped to an OS path on the host (``Path``) for the local impl,
or to the container's filesystem for Docker. Implementations call
:func:`resolve` to turn a workspace path into a host :class:`Path` while
rejecting traversal attempts (``..``, absolute escapes, symlink jumps).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .errors import PathEscape

__all__ = ["normalize", "resolve"]


def normalize(path: str, *, workspace: str = "/workspace") -> str:
    """Return ``path`` as a workspace-relative POSIX string.

    Absolute paths inside ``workspace`` are stripped of the workspace prefix.
    Relative paths are returned unchanged. ``""`` and ``"."`` become ``"."``.
    Traversal escapes (``..``, absolute outside workspace) raise
    :class:`PathEscape`.
    """
    if not path or path == ".":
        return "."

    pp = PurePosixPath(path)
    ws = PurePosixPath(workspace)

    if pp.is_absolute():
        try:
            rel = pp.relative_to(ws)
        except ValueError as exc:
            raise PathEscape(
                f"Path {path!r} is absolute but outside workspace {workspace!r}.",
                hint=f"Use a path under {workspace} or a relative path.",
            ) from exc
        pp = PurePosixPath(rel)

    parts = pp.parts
    if any(part == ".." for part in parts):
        raise PathEscape(
            f"Path {path!r} contains a '..' traversal.",
            hint="Use sandbox-relative paths without '..'.",
        )
    return str(pp) if parts else "."


def resolve(root: Path, path: str, *, workspace: str = "/workspace") -> Path:
    """Resolve a workspace path to a real host :class:`Path` under ``root``.

    Combines :func:`normalize` with a `resolved-under-root` symlink check so
    a symlink inside ``root`` that points outside is also rejected.
    """
    rel = normalize(path, workspace=workspace)
    if rel == ".":
        return root
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PathEscape(
            f"Path {path!r} resolves outside the sandbox root {root}.",
            hint="Check for symlinks pointing outside the workspace.",
        ) from exc
    return target
