"""Unit tests for ``lovia.workspace.paths`` — relative-path normalization and
root confinement. This module had no dedicated test file before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lovia.workspace import PathOutsideWorkspaceError
from lovia.workspace.paths import (
    ensure_inside,
    normalize_relative_path,
    resolve_existing,
    resolve_parent,
)


# ------------------------------------------------------ normalize_relative_path


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "."),
        (".", "."),
        ("src/app.py", "src/app.py"),
        ("./a/./b", "a/b"),  # "" and "." segments are dropped
        ("a//b", "a/b"),  # empty segment from double slash
        ("a/../b", "b"),  # parent pops the previous segment
        ("a/b/..", "a"),
        ("./", "."),  # nothing but dot segments
    ],
)
def test_normalize_relative_path(raw: str, expected: str) -> None:
    assert normalize_relative_path(raw) == expected


def test_normalize_rejects_absolute() -> None:
    with pytest.raises(PathOutsideWorkspaceError, match="absolute"):
        normalize_relative_path("/etc/passwd")


@pytest.mark.parametrize("raw", ["../x", "a/../../x", ".."])
def test_normalize_rejects_escape(raw: str) -> None:
    with pytest.raises(PathOutsideWorkspaceError, match="escapes"):
        normalize_relative_path(raw)


# ---------------------------------------------------------------- ensure_inside


def test_ensure_inside_accepts_child(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "f.txt"
    assert ensure_inside(tmp_path, target, "sub/f.txt") == target.resolve()


def test_ensure_inside_rejects_sibling(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere"
    with pytest.raises(PathOutsideWorkspaceError, match="outside the workspace"):
        ensure_inside(tmp_path, outside, "../elsewhere")


# -------------------------------------------------------------- resolve_existing


def test_resolve_existing_returns_root_for_dot(tmp_path: Path) -> None:
    assert resolve_existing(tmp_path, ".") == tmp_path.resolve()


def test_resolve_existing_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    assert resolve_existing(tmp_path, "a.txt") == (tmp_path / "a.txt").resolve()


# ---------------------------------------------------------------- resolve_parent


def test_resolve_parent_refuses_root_as_file(tmp_path: Path) -> None:
    with pytest.raises(PathOutsideWorkspaceError, match="root as a file"):
        resolve_parent(tmp_path, ".")


def test_resolve_parent_allows_missing_dirs(tmp_path: Path) -> None:
    parent, name = resolve_parent(tmp_path, "new/deep/file.txt")
    assert name == "file.txt"
    assert parent == tmp_path / "new" / "deep"  # not created, just resolved


def test_resolve_parent_existing_dir(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    parent, name = resolve_parent(tmp_path, "sub/file.txt")
    assert name == "file.txt"
    assert parent == (tmp_path / "sub").resolve()
