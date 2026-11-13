"""grep and globbed list_files follow git's ignore rules in a work tree."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lovia.workspace import LocalWorkspaceSession

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "protocol.file.allow=always",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _repo(path: Path, committed: dict[str, str]) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _write(path, committed)
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


def _project(tmp_path: Path) -> Path:
    """Ignores node_modules/ and *.log; pkg/gone.py is deleted, uncommitted;
    src/new.py is untracked."""
    root = _repo(
        tmp_path / "repo",
        {
            ".gitignore": "node_modules/\n*.log\n",
            "top.py": "TOKEN\n",
            "pkg/mod.py": "TOKEN\n",
            "pkg/gone.py": "TOKEN\n",
        },
    )
    (root / "pkg" / "gone.py").unlink()
    _write(
        root,
        {
            "src/new.py": "TOKEN\n",
            "node_modules/dep/index.js": "TOKEN\n",
            "debug.log": "TOKEN\n",
        },
    )
    return root


async def _grep(session: LocalWorkspaceSession, path: str = ".") -> list[str]:
    return [m.path for m in await session.grep("TOKEN", path=path)]


async def _glob(
    session: LocalWorkspaceSession,
    pattern: str,
    path: str = ".",
    include_hidden: bool = False,
) -> list[str]:
    entries = await session.list_files(
        path, pattern=pattern, include_hidden=include_hidden
    )
    return [e.path for e in entries]


async def test_ignored_files_drop_out_and_untracked_ones_stay(tmp_path) -> None:
    session = LocalWorkspaceSession(root=str(_project(tmp_path)))
    # The walk's order: a directory's files, then its subdirectories.
    assert await _grep(session) == ["top.py", "pkg/mod.py", "src/new.py"]
    # Directories come with their files; an ignored one does not.
    assert await _glob(session, "**/*") == [
        "pkg",
        "pkg/mod.py",
        "src",
        "src/new.py",
        "top.py",
    ]


async def test_glob_segments_and_cap(tmp_path) -> None:
    session = LocalWorkspaceSession(root=str(_project(tmp_path)))
    assert await _glob(session, "*.py") == ["top.py"]
    assert await _glob(session, "**/*.py") == ["pkg/mod.py", "src/new.py", "top.py"]
    assert await _glob(session, "pkg/*") == ["pkg/mod.py"]
    assert await _glob(session, "*/") == ["pkg", "src"]
    assert await _glob(session, "*", include_hidden=True) == [
        ".gitignore",
        "pkg",
        "src",
        "top.py",
    ]
    # Filtered first, then cut: the cap keeps the first paths in order.
    clipped = await session.list_files(".", pattern="**/*", max_results=2)
    assert [e.path for e in clipped] == ["pkg", "pkg/mod.py"]
    assert getattr(clipped, "truncated", False) is True


async def test_naming_an_ignored_directory_searches_it(tmp_path) -> None:
    session = LocalWorkspaceSession(root=str(_project(tmp_path)))
    assert await _grep(session, "node_modules") == ["node_modules/dep/index.js"]
    assert await _grep(session, "node_modules/dep") == ["node_modules/dep/index.js"]
    assert await _glob(session, "**/*.js", "node_modules") == [
        "node_modules/dep/index.js"
    ]


async def test_submodule_and_nested_repository_files_stay(tmp_path) -> None:
    lib = _repo(tmp_path / "lib", {"lib.py": "TOKEN\n"})
    root = _project(tmp_path)
    _git(root, "submodule", "add", "-q", str(lib), "sub")
    nested = _repo(root / "nested", {".gitignore": "*.tmp\n", "n.py": "TOKEN\n"})
    _write(nested, {"scratch.tmp": "TOKEN\n"})
    session = LocalWorkspaceSession(root=str(root))
    hits = await _grep(session)
    assert "sub/lib.py" in hits
    assert "nested/n.py" in hits
    assert "nested/scratch.tmp" not in hits  # the nested repository's own rules
    assert "sub/lib.py" in await _glob(session, "**/*.py")


async def test_tracked_directory_swapped_for_a_symlink_is_not_entered(
    tmp_path,
) -> None:
    # The index still lists pkg/mod.py; on disk pkg now leads outside the
    # root. The walk never enters a symlinked directory, nor may git's list.
    root = _project(tmp_path)
    outside = tmp_path / "outside"
    _write(outside, {"mod.py": "TOKEN\n"})
    shutil.rmtree(root / "pkg")
    (root / "pkg").symlink_to(outside, target_is_directory=True)
    session = LocalWorkspaceSession(root=str(root))
    assert await _grep(session) == ["top.py", "src/new.py"]
    assert "pkg/mod.py" not in await _glob(session, "**/*")


async def test_symlinks_git_lists_are_judged_by_their_targets(tmp_path) -> None:
    # Git lists a symlink as a file of its own; the listing still follows it:
    # hidden when it leads to a denied file, kept with its target when it
    # merely leads outside, a directory when it leads to one.
    from lovia.workspace import WorkspacePolicy

    root = _project(tmp_path)
    outside = tmp_path / "outside"
    _write(outside, {"notes.txt": "x\n", "creds.env": "KEY=1\n"})
    (root / "to-notes").symlink_to(outside / "notes.txt")
    (root / "to-creds").symlink_to(outside / "creds.env")
    (root / "to-pkg").symlink_to(root / "pkg", target_is_directory=True)
    session = LocalWorkspaceSession(
        root=str(root),
        policy=WorkspacePolicy(denied_paths=("*.env",), read_outside="ask"),
    )
    entries = {e.path: e for e in await session.list_files(".", pattern="*")}
    assert "to-creds" not in entries
    notes = entries["to-notes"]
    assert notes.symlink_target == (outside / "notes.txt").resolve().as_posix()
    assert (notes.is_dir, notes.size) == (False, 2)
    assert entries["to-pkg"].is_dir is True
    # A plain file: its size and mtime as stat reports them.
    top = entries["top.py"]
    st = (root / "top.py").stat()
    assert (top.is_dir, top.size, top.mtime, top.symlink_target) == (
        False,
        st.st_size,
        st.st_mtime,
        None,
    )


async def test_without_git_the_tree_is_walked(tmp_path, monkeypatch) -> None:
    root = _project(tmp_path)
    monkeypatch.setenv("PATH", "")
    session = LocalWorkspaceSession(root=str(root))
    assert "node_modules/dep/index.js" in await _grep(session)
    assert "node_modules" in await _glob(session, "*")
