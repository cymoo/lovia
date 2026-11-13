"""Project instructions: the ``AGENTS.md`` files from the Git root down to a
chat's directory, as :func:`~lovia.workspace.local.instruction_files` finds
and reads them — the one function both a local chat and a remote host use."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lovia.workspace import PathRule, WorkspacePolicy
from lovia.workspace.local import (
    INSTRUCTIONS_MAX_BYTES,
    LocalWorkspaceSession,
    instruction_files,
)


def _tree(tmp_path: Path) -> Path:
    """``repo`` (a Git root) with rules at the top and in ``pkg``; the chat
    works in ``repo/pkg/sub``, which has none of its own."""
    repo = tmp_path.resolve() / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "pkg" / "sub").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("repo rules", encoding="utf-8")
    (repo / "pkg" / "AGENTS.md").write_text("pkg rules", encoding="utf-8")
    return repo


def _used(files) -> list[tuple[str, int, str]]:
    return [(f.path, f.level, f.text) for f in files if not f.skipped]


def _no_git_above(path: Path) -> bool:
    return not any((d / ".git").exists() for d in path.resolve().parents)


def test_a_chat_at_the_git_root_reads_its_own_file_alone(tmp_path: Path) -> None:
    repo = _tree(tmp_path)
    got = instruction_files(repo, WorkspacePolicy())
    assert _used(got) == [(f"{repo.as_posix()}/AGENTS.md", 0, "repo rules")]


def test_a_chat_below_the_git_root_reads_every_level_outermost_first(
    tmp_path: Path,
) -> None:
    repo = _tree(tmp_path)
    got = instruction_files(repo / "pkg" / "sub", WorkspacePolicy())
    assert _used(got) == [
        (f"{repo.as_posix()}/AGENTS.md", 2, "repo rules"),
        (f"{repo.as_posix()}/pkg/AGENTS.md", 1, "pkg rules"),
    ]


def test_without_a_git_root_only_the_chat_directory_counts(tmp_path: Path) -> None:
    if not _no_git_above(tmp_path):
        pytest.skip("the temporary directory is inside a Git work tree")
    top = tmp_path.resolve() / "top"
    (top / "pkg").mkdir(parents=True)
    (top / "AGENTS.md").write_text("above", encoding="utf-8")
    (top / "pkg" / "AGENTS.md").write_text("here", encoding="utf-8")
    assert [f.text for f in instruction_files(top / "pkg", WorkspacePolicy())] == [
        "here"
    ]


def test_a_git_file_marks_the_root_and_the_nearest_one_wins(tmp_path: Path) -> None:
    # A worktree or submodule has a .git *file*; it is a root all the same,
    # and the walk stops there — the enclosing repository's rules are its own.
    outer = _tree(tmp_path)
    inner = outer / "pkg" / "sub"
    (inner / ".git").write_text("gitdir: ../../.git/modules/sub\n", encoding="utf-8")
    (inner / "AGENTS.md").write_text("sub rules", encoding="utf-8")
    (inner / "deep").mkdir()
    got = instruction_files(inner / "deep", WorkspacePolicy())
    assert _used(got) == [(f"{inner.as_posix()}/AGENTS.md", 1, "sub rules")]


def test_read_only_still_reads_up_the_tree(tmp_path: Path) -> None:
    # The default outside-root verdict decides what the agent explores, not
    # what loads as its instructions: "deny" there changes nothing here.
    repo = _tree(tmp_path)
    where = repo / "pkg" / "sub"
    assert WorkspacePolicy.readonly().read_outside == "deny"
    assert instruction_files(where, WorkspacePolicy.readonly()) == instruction_files(
        where, WorkspacePolicy.trusted()
    )


def test_a_denied_ancestor_is_skipped_and_says_why(tmp_path: Path) -> None:
    repo = _tree(tmp_path)
    policy = WorkspacePolicy.readonly(denied_paths=(f"{repo.as_posix()}/AGENTS.md",))
    top, pkg = instruction_files(repo / "pkg" / "sub", policy)
    assert (top.path, top.text, top.skipped) == (
        f"{repo.as_posix()}/AGENTS.md",
        "",
        "a workspace rule covers it",
    )
    assert (pkg.text, pkg.skipped) == ("pkg rules", "")


def test_a_rule_that_asks_hides_and_one_that_allows_does_not(tmp_path: Path) -> None:
    # Nobody is there to answer an ask; an explicit allow is the rule's own word.
    repo = _tree(tmp_path)
    where = repo / "pkg" / "sub"
    ask = WorkspacePolicy(path_rules=(PathRule(f"{repo.as_posix()}/AGENTS.md", "ask"),))
    assert [f.text for f in instruction_files(where, ask) if not f.skipped] == [
        "pkg rules"
    ]
    allow = WorkspacePolicy.readonly(
        path_rules=(PathRule(f"{repo.as_posix()}/AGENTS.md", "allow"),)
    )
    assert len(_used(instruction_files(where, allow))) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
def test_a_link_is_judged_where_it_leads(tmp_path: Path) -> None:
    repo = _tree(tmp_path)
    secrets = tmp_path.resolve() / "secrets"
    secrets.mkdir()
    (secrets / "token").write_text("TOKEN", encoding="utf-8")
    shared = tmp_path.resolve() / "shared.md"
    shared.write_text("shared rules", encoding="utf-8")
    policy = WorkspacePolicy.readonly(denied_paths=(f"{secrets.as_posix()}/**",))

    # An AGENTS.md linking into a denied directory is left out, wherever on
    # the way up it sits; one linking to an ordinary file elsewhere loads,
    # under the name it has in the tree.
    (repo / "pkg" / "AGENTS.md").unlink()
    (repo / "pkg" / "AGENTS.md").symlink_to(secrets / "token")
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").symlink_to(shared)
    top, pkg = instruction_files(repo / "pkg" / "sub", policy)
    assert (top.path, top.text) == (f"{repo.as_posix()}/AGENTS.md", "shared rules")
    assert (pkg.text, pkg.skipped) == ("", "a workspace rule covers it")

    # The chat directory's own file, too.
    (repo / "pkg" / "sub" / "AGENTS.md").symlink_to(secrets / "token")
    assert "TOKEN" not in str(instruction_files(repo / "pkg" / "sub", policy))


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
def test_a_directory_reached_through_a_link_walks_its_real_ancestors(
    tmp_path: Path,
) -> None:
    # outer/link → elsewhere/proj: a chat in outer/link/pkg is in
    # elsewhere/proj/pkg, and only that tree's rules apply — as Git sees it.
    outer = tmp_path.resolve() / "outer"
    (outer / ".git").mkdir(parents=True)
    (outer / "AGENTS.md").write_text("outer rules", encoding="utf-8")
    proj = tmp_path.resolve() / "elsewhere" / "proj"
    (proj / ".git").mkdir(parents=True)
    (proj / "pkg").mkdir()
    (proj / "AGENTS.md").write_text("proj rules", encoding="utf-8")
    (outer / "link").symlink_to(proj, target_is_directory=True)
    got = instruction_files(outer / "link" / "pkg", WorkspacePolicy())
    assert _used(got) == [(f"{proj.as_posix()}/AGENTS.md", 1, "proj rules")]

    # And a denied directory reached through a link stays denied.
    policy = WorkspacePolicy(denied_paths=(f"{proj.as_posix()}/**",))
    assert _used(instruction_files(outer / "link" / "pkg", policy)) == []


def test_the_size_cap_fills_from_the_chat_directory_outwards(tmp_path: Path) -> None:
    repo = _tree(tmp_path)
    half = INSTRUCTIONS_MAX_BYTES // 2
    (repo / "pkg" / "AGENTS.md").write_text("p" * (half + 1), encoding="utf-8")
    (repo / "AGENTS.md").write_text("r" * half, encoding="utf-8")
    top, pkg = instruction_files(repo / "pkg" / "sub", WorkspacePolicy())
    # The nearer file fits; the outer one would take the total past the cap
    # and is left out whole — never cut mid-file.
    assert len(pkg.text) == half + 1 and not pkg.skipped
    assert top.text == "" and "262,144 bytes" in top.skipped

    # Exactly at the cap is within it.
    (repo / "AGENTS.md").write_text("r" * (half - 1), encoding="utf-8")
    assert len(_used(instruction_files(repo / "pkg" / "sub", WorkspacePolicy()))) == 2

    # One file over the cap on its own never loads.
    (repo / "AGENTS.md").write_text(
        "r" * (INSTRUCTIONS_MAX_BYTES + 1), encoding="utf-8"
    )
    (only,) = instruction_files(repo, WorkspacePolicy())
    assert only.skipped and only.text == ""


def test_not_a_file_and_undecodable_bytes(tmp_path: Path) -> None:
    repo = _tree(tmp_path)
    (repo / "pkg" / "AGENTS.md").unlink()
    (repo / "pkg" / "AGENTS.md").mkdir()  # a directory is no instructions
    (repo / "AGENTS.md").write_bytes(b"caf\xe9")
    assert _used(instruction_files(repo / "pkg", WorkspacePolicy())) == [
        (f"{repo.as_posix()}/AGENTS.md", 1, "caf�")
    ]


async def test_the_session_reads_its_own_root_under_its_own_policy(
    tmp_path: Path,
) -> None:
    repo = _tree(tmp_path)
    policy = WorkspacePolicy.readonly(denied_paths=(f"{repo.as_posix()}/AGENTS.md",))
    session = LocalWorkspaceSession(repo / "pkg" / "sub", policy=policy)
    try:
        assert await session.read_instructions() == instruction_files(
            repo / "pkg" / "sub", policy
        )
    finally:
        await session.close()
