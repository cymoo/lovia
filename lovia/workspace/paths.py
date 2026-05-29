"""Workspace path utilities."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .errors import PathEscape

__all__ = ["normalize", "resolve"]


def normalize(path: str, *, workspace: str = "/workspace") -> str:
    """Return ``path`` as a workspace-relative POSIX string."""

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
            hint="Use workspace-relative paths without '..'.",
        )
    return str(pp) if parts else "."


def resolve(root: Path, path: str, *, workspace: str = "/workspace") -> Path:
    """Resolve a workspace path to a host path under ``root``."""

    root = root.resolve()
    rel = normalize(path, workspace=workspace)
    if rel == ".":
        return root
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PathEscape(
            f"Path {path!r} resolves outside the workspace root {root}.",
            hint="Check for symlinks pointing outside the workspace.",
        ) from exc
    return target
