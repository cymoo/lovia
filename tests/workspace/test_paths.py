"""Unit tests for ``lovia.workspace.paths`` — resolution and classification.

The old model rejected absolute paths and root escapes at the path layer;
the new one resolves everything (symlinks followed) and classifies the
result as inside/outside the root, leaving the verdict to the policy ACL.
"""

from __future__ import annotations

import os
from pathlib import Path

from lovia.workspace.paths import resolve_path


def test_relative_path_resolves_under_root(tmp_path: Path) -> None:
    rp = resolve_path(tmp_path, "src/app.py")
    assert rp.abs == tmp_path / "src" / "app.py"
    assert rp.rel == "src/app.py"
    assert rp.inside
    assert rp.display() == "src/app.py"


def test_empty_and_dot_resolve_to_root(tmp_path: Path) -> None:
    for raw in ("", "."):
        rp = resolve_path(tmp_path, raw)
        assert rp.abs == tmp_path
        assert rp.rel == "."


def test_dot_segments_normalize(tmp_path: Path) -> None:
    rp = resolve_path(tmp_path, "./a/./b//c")
    assert rp.rel == "a/b/c"


def test_absolute_path_inside_root_is_classified_inside(tmp_path: Path) -> None:
    rp = resolve_path(tmp_path, str(tmp_path / "a.txt"))
    assert rp.rel == "a.txt"
    assert rp.display() == "a.txt"


def test_absolute_path_outside_root_is_classified_outside(tmp_path: Path) -> None:
    rp = resolve_path(tmp_path, "/etc/hosts")
    assert rp.rel is None
    assert not rp.inside
    # /etc may itself be a symlink (macOS: /private/etc) — display shows the
    # resolved target.
    assert rp.display() == Path("/etc/hosts").resolve().as_posix()


def test_dotdot_escape_is_classified_not_rejected(tmp_path: Path) -> None:
    rp = resolve_path(tmp_path, "../sibling.txt")
    assert rp.rel is None
    assert rp.abs == tmp_path.parent / "sibling.txt"


def test_dotdot_within_root_stays_inside(tmp_path: Path) -> None:
    rp = resolve_path(tmp_path, "a/../b")
    assert rp.rel == "b"


def test_tilde_expands_to_home(tmp_path: Path) -> None:
    rp = resolve_path(tmp_path, "~/notes.txt")
    assert rp.abs == Path.home().resolve() / "notes.txt"
    assert rp.raw == "~/notes.txt"


def test_symlink_resolves_to_target(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    outside = tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    (outside / "real.txt").write_text("x", encoding="utf-8")
    os.symlink(outside / "real.txt", root / "link.txt")
    rp = resolve_path(root, "link.txt")
    assert rp.rel is None  # judged by where it leads
    assert rp.abs == (outside / "real.txt").resolve()


def test_missing_tail_resolves_lexically(tmp_path: Path) -> None:
    # Write targets that don't exist yet still resolve (non-strict).
    rp = resolve_path(tmp_path, "new/deep/file.txt")
    assert rp.rel == "new/deep/file.txt"
    assert not rp.abs.exists()


def test_base_overrides_root_for_relative_paths(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    rp = resolve_path(tmp_path, "f.txt", base=sub)
    assert rp.rel == "sub/f.txt"
