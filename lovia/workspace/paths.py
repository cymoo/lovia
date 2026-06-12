"""Path normalization and confinement for workspace file operations."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .errors import PathOutsideWorkspaceError

__all__ = [
    "ensure_inside",
    "normalize_relative_path",
    "resolve_existing",
    "resolve_parent",
]


def normalize_relative_path(path: str) -> str:
    """Return a normalized workspace-relative POSIX path.

    Absolute paths are rejected. ``..`` segments are allowed only when they do
    not escape above the workspace root.
    """

    if not path or path == ".":
        return "."
    pp = PurePosixPath(path)
    if pp.is_absolute():
        raise PathOutsideWorkspaceError(
            f"Path {path!r} is absolute.",
            hint="Use a path relative to the workspace root, e.g. 'src/app.py'.",
        )

    parts: list[str] = []
    for part in pp.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise PathOutsideWorkspaceError(
                    f"Path {path!r} escapes the workspace root.",
                    hint="Use a path relative to the workspace root.",
                )
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) if parts else "."


def ensure_inside(root: Path, target: Path, original: str) -> Path:
    """Return ``target`` if it resolves under ``root``, else raise."""

    root = root.resolve()
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathOutsideWorkspaceError(
            f"Path {original!r} resolves outside the workspace root {root}.",
            hint="Check for symlinks pointing outside the workspace root.",
        ) from exc
    return resolved


def resolve_existing(root: Path, path: str) -> Path:
    """Resolve an existing workspace-relative path under ``root``."""

    rel = normalize_relative_path(path)
    target = root if rel == "." else root / rel
    return ensure_inside(root, target, path)


def resolve_parent(root: Path, path: str) -> tuple[Path, str]:
    """Resolve the parent directory for a write path under ``root``.

    Missing parent directories are permitted, but any existing symlink in the
    parent chain must still resolve inside ``root``.
    """

    rel = normalize_relative_path(path)
    if rel == ".":
        raise PathOutsideWorkspaceError(
            "Cannot write to the workspace root as a file.",
            hint="Provide a file path relative to the workspace root.",
        )
    parts = PurePosixPath(rel).parts
    parent = root
    for part in parts[:-1]:
        candidate = parent / part
        if candidate.exists() or candidate.is_symlink():
            parent = ensure_inside(root, candidate, path)
        else:
            parent = candidate
    ensure_inside(root, parent if parent.exists() else parent.parent, path)
    target = parent / parts[-1]
    if target.exists() or target.is_symlink():
        ensure_inside(root, target, path)
    return parent, parts[-1]
