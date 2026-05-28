"""Path resolution + traversal guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from lovia.sandbox.errors import PathEscape
from lovia.sandbox.paths import normalize, resolve


def test_normalize_relative() -> None:
    assert normalize("src/app.py") == "src/app.py"
    assert normalize("") == "."
    assert normalize(".") == "."


def test_normalize_absolute_inside_workspace() -> None:
    assert normalize("/workspace/src/app.py") == "src/app.py"
    assert normalize("/workspace") == "."


def test_normalize_absolute_outside_workspace() -> None:
    with pytest.raises(PathEscape):
        normalize("/etc/passwd")


def test_normalize_rejects_traversal() -> None:
    for bad in ("..", "../etc", "sub/../../etc", "/workspace/../etc"):
        with pytest.raises(PathEscape):
            normalize(bad)


def test_resolve_under_root(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    assert resolve(tmp_path, "x") == (tmp_path / "x").resolve()
    assert resolve(tmp_path, ".") == tmp_path.resolve()
    assert resolve(tmp_path, "/workspace/x") == (tmp_path / "x").resolve()


def test_resolve_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    inside_root = tmp_path / "root"
    inside_root.mkdir()
    (inside_root / "link").symlink_to(outside)
    with pytest.raises(PathEscape):
        resolve(inside_root, "link/secret")
