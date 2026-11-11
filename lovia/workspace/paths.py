"""Path resolution and classification for workspace file operations.

The old model rejected everything that escaped the workspace root; this one
*classifies* instead: any input path — workspace-relative, absolute, or
``~``-prefixed — is resolved (symlinks followed) and tagged as inside or
outside the root. What a resolved path may be used for is then a policy
question (:meth:`WorkspacePolicy.decide_path`), not a path-syntax one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ResolvedPath", "complete_dirs", "resolve_path"]

# A completion lists this many directories at most, from at most this many
# entries read (a huge directory must not stall the request).
_MAX_SUGGESTIONS = 50
_MAX_SCANNED = 5000


def complete_dirs(raw: str, roots: tuple[Path, ...] | None = None) -> list[str]:
    """Subdirectories completing ``raw`` (a path being typed), as typed.

    For a folder picker, not for the agent: nothing here consults a policy.
    ``raw`` ending in a separator lists that directory; otherwise its parent,
    filtered by the last segment (case-insensitively). Hidden directories
    show only once a dot is typed. ``roots`` limits the tree: a suggestion
    must lie under one of them or on the way to one.
    """
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return []
    # Split on the last separator by hand: Path would normalize a trailing
    # "." (a hidden name being typed) away.
    cut = max(expanded.rfind("/"), expanded.rfind(os.sep)) + 1
    parent, prefix = expanded[:cut], expanded[cut:]
    head = raw[: len(raw) - len(prefix)]
    found: list[str] = []
    try:
        with os.scandir(parent) as it:
            for n, entry in enumerate(it):
                if n >= _MAX_SCANNED:
                    break
                name = entry.name
                if name.startswith(".") and not prefix.startswith("."):
                    continue
                if not name.lower().startswith(prefix.lower()):
                    continue
                if not entry.is_dir():
                    continue
                if roots is not None:
                    path = Path(entry.path).resolve()
                    if not any(
                        path.is_relative_to(r) or r.is_relative_to(path) for r in roots
                    ):
                        continue
                found.append(name)
    except OSError:
        return []
    found.sort(key=str.lower)
    return [head + name for name in found[:_MAX_SUGGESTIONS]]


@dataclass(frozen=True)
class ResolvedPath:
    """A fully resolved path plus its relation to the workspace root."""

    raw: str
    """The original input string (as the model supplied it)."""

    abs: Path
    """The resolved absolute path. Symlinks in existing components are
    followed; a missing tail is appended lexically (non-strict resolution),
    so write targets that don't exist yet still resolve."""

    rel: str | None
    """The workspace-relative POSIX path when ``abs`` is inside the root
    (``"."`` for the root itself); ``None`` when outside."""

    @property
    def inside(self) -> bool:
        return self.rel is not None

    @property
    def abs_posix(self) -> str:
        return self.abs.as_posix()

    def display(self) -> str:
        """Workspace-relative form when inside the root, absolute otherwise."""
        return self.rel if self.rel is not None else self.abs.as_posix()


def resolve_path(root: Path, path: str, *, base: Path | None = None) -> ResolvedPath:
    """Resolve ``path`` against the workspace and classify it.

    ``path`` may be workspace-relative (resolved against ``base``, which
    defaults to ``root``), absolute, or start with ``~``. ``..`` segments and
    symlinks are not rejected — they are resolved, and the resulting target
    is what the policy judges. ``root`` must already be resolved (the session
    resolves it once at construction).
    """
    raw = path
    if not path:
        path = "."
    p = Path(path)
    if path.startswith("~"):
        try:
            p = p.expanduser()
        except RuntimeError:
            # No resolvable home directory — leave the path literal; the
            # operation will fail later with a clear "not a file" error.
            pass
    if not p.is_absolute():
        p = (base if base is not None else root) / p
    resolved = p.resolve()
    try:
        rel = resolved.relative_to(root).as_posix()
    except ValueError:
        rel = None
    return ResolvedPath(raw=raw, abs=resolved, rel=rel)
