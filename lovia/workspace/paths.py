"""Path resolution and classification for workspace file operations.

The old model rejected everything that escaped the workspace root; this one
*classifies* instead: any input path — workspace-relative, absolute, or
``~``-prefixed — is resolved (symlinks followed) and tagged as inside or
outside the root. What a resolved path may be used for is then a policy
question (:meth:`WorkspacePolicy.decide_path`), not a path-syntax one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["ResolvedPath", "resolve_path"]


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
