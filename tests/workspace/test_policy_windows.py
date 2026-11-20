"""Paths in the host's native form, run for real on Windows CI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from lovia.approvals import Verdict
from lovia.workspace import PathRule, WorkspacePolicy
from lovia.workspace.command_guard import extract_path_claims
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
    assert session.decide_path(str(outside / "sub" / "a.txt")).action == "allow"
    assert session.decide_path(str(tmp_path / "elsewhere.txt")).action == "deny"


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
    assert session.decide_path(str(outside / "sub" / "a.txt")).action == "deny"
    assert session.decide_path(str(tmp_path / "elsewhere.txt")).action == "allow"


@pytest.mark.skipif(os.name == "nt", reason="C: is a drive on Windows")
def test_drive_like_pattern_stays_workspace_relative_on_posix() -> None:
    # On POSIX "C:" is an ordinary directory name, as resolve_path sees it.
    policy = WorkspacePolicy(read_outside="allow", denied_paths=("C:/data",))
    decide = policy.decide_path
    assert (
        decide(rel="C:/data/x", abs_posix="/ws/C:/data/x", op="read").action == "deny"
    )
    assert decide(rel=None, abs_posix="/srv/C:/data/x", op="read").action == "allow"


@windows_only
@pytest.mark.parametrize(
    "command, claim",
    [
        (r"type C:\data\a.txt", ("read", r"C:\data\a.txt")),
        (r'type "C:\Program Files\a.txt"', ("read", r"C:\Program Files\a.txt")),
        (r"type \\srv\share\a.txt", ("read", r"\\srv\share\a.txt")),
        (r"type ..\..\a.txt", ("read", r"..\..\a.txt")),
        (r"type data\a", ("read", r"data\a")),
        (r"dir > C:\out.txt", ("write", r"C:\out.txt")),
        (r"dd of=C:\x", ("read", r"C:\x")),
        ("dd of=C:/x", ("read", "C:/x")),
        (r"tool --out=C:\x", ("read", r"C:\x")),
    ],
)
def test_shell_claims_keep_the_host_path(command: str, claim: tuple[str, str]) -> None:
    assert extract_path_claims(command) == [claim]


@windows_only
@spellings
def test_shell_command_is_judged_on_a_drive_path(
    tmp_path: Path, spell: Callable[[Path], str]
) -> None:
    policy = WorkspacePolicy(
        read_outside="ask", write_outside="deny", shell_default="allow"
    )
    session = _session(tmp_path, policy)
    target = spell(tmp_path / "outside" / "a.txt")
    assert session.decide_command(f"type {target}").action == "ask"
    assert session.decide_command(f"dd of={target}").action == "ask"
    assert session.decide_command(f"dir > {target}").action == "deny"
    assert (
        session.decide_command(f"dir > {spell(tmp_path / 'ws' / 'a.txt')}").action
        == "allow"
    )


@windows_only
def test_a_quoted_multi_line_script_offers_no_grant_per_line() -> None:
    # Cut at each quoted line break, the script's lines passed for commands:
    # "always allow import, try:, print, except, \"".
    policy = WorkspacePolicy()
    script = (
        'python -c "\nimport sys\ntry:\n    print(1)\nexcept ImportError:\n    pass\n"'
    )
    assert policy.decide_command(script) == Verdict("ask")
    assert policy.decide_command('python -c "print(1)"').scopes == ("shell:python",)
    # A break between commands is still a cut, each with its own grant.
    assert policy.decide_command("git status\ndir").scopes == ("shell:git", "shell:dir")


@windows_only
async def test_cmd_runs_only_the_first_line_of_a_command(tmp_path: Path) -> None:
    # What the workspace prompt tells the model about cmd.exe, checked.
    session = _session(tmp_path, WorkspacePolicy(shell_default="allow"))
    result = await session.run("echo first\necho second")
    assert "first" in result.stdout
    assert "second" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="backslash is a separator on Windows")
def test_shell_claims_keep_posix_tokenization() -> None:
    assert extract_path_claims(r"cat sub\ dir/a") == [("read", "sub dir/a")]
    assert extract_path_claims(r"type C:\data\a.txt") == [("read", "C:dataa.txt")]
    assert extract_path_claims("dd of=C:/x") == [("read", "of=C:/x")]
