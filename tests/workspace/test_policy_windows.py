"""Path patterns in the host's absolute form, run for real on Windows CI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from lovia.workspace import PathRule, WorkspacePolicy
from lovia.workspace.local import LocalWorkspaceSession

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
spellings = pytest.mark.parametrize(
    "spell", [Path.as_posix, str], ids=["forward-slash", "backslash"]
)


def _session(tmp_path: Path, policy: WorkspacePolicy) -> LocalWorkspaceSession:
    root = tmp_path / "ws"
    root.mkdir()
    return LocalWorkspaceSession(root=root, policy=policy)


@windows_only
@spellings
def test_drive_pattern_grants_a_path_outside_the_root(
    tmp_path: Path, spell: Callable[[Path], str]
) -> None:
    outside = tmp_path / "outside"
    policy = WorkspacePolicy(path_rules=(PathRule(spell(outside), "allow"),))
    session = _session(tmp_path, policy)
    assert session.decide_path(str(outside / "sub" / "a.txt")) == "allow"
    assert session.decide_path(str(tmp_path / "elsewhere.txt")) == "deny"


@windows_only
@spellings
def test_drive_glob_denies_a_path_outside_the_root(
    tmp_path: Path, spell: Callable[[Path], str]
) -> None:
    outside = tmp_path / "outside"
    policy = WorkspacePolicy(
        read_outside="allow", denied_paths=(spell(outside / "**"),)
    )
    session = _session(tmp_path, policy)
    assert session.decide_path(str(outside / "sub" / "a.txt")) == "deny"
    assert session.decide_path(str(tmp_path / "elsewhere.txt")) == "allow"


@pytest.mark.skipif(os.name == "nt", reason="C: is a drive on Windows")
def test_drive_like_pattern_stays_workspace_relative_on_posix() -> None:
    # On POSIX "C:" is an ordinary directory name, as resolve_path sees it.
    policy = WorkspacePolicy(read_outside="allow", denied_paths=("C:/data",))
    decide = policy.decide_path
    assert decide(rel="C:/data/x", abs_posix="/ws/C:/data/x", op="read") == "deny"
    assert decide(rel=None, abs_posix="/srv/C:/data/x", op="read") == "allow"
