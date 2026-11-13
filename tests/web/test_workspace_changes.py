"""The Files panel's change list and diff over a real git work tree
(``/api/workspace/changes``, ``/api/workspace/diff``, :mod:`lovia.web.changes`)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.workspace import (  # noqa: E402
    PermissionDeniedError,
    Verdict,
    Workspace,
    WorkspaceError,
    WorkspaceLimits,
)
from lovia.workspace.types import CommandResult  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.changes import file_diff, parse_numstat, parse_status  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402
from lovia.web.workspaces import open_view  # noqa: E402

from ..scripted_provider import ScriptedProvider, text  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _client(root: Path, **kw) -> TestClient:
    ws = Workspace.local(str(root), denied_paths=("secrets.env",), **kw)
    bot = Agent(name="bot", model=ScriptedProvider([text("hi")]), workspace=ws)
    return TestClient(create_app(bot, store=ChatStore.in_memory(), titles=False))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A committed tree, then: one edit, one delete, a new file, a new
    directory, a denied file changed and a scratch file changed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tmp").mkdir()
    for name in ("a.txt", "b.txt", "secrets.env", "tmp/x.txt"):
        (repo / name).write_text("old\nsame\n")
    (repo / "pic.bin").write_bytes(b"\x00\x01")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "a.txt").write_text("new\nsame\nmore\n")
    (repo / "b.txt").unlink()
    (repo / "new.txt").write_text("hello\n")
    (repo / "newdir").mkdir()
    (repo / "newdir" / "f.txt").write_text("f\n")
    (repo / "secrets.env").write_text("KEY=leak\n")
    (repo / "tmp" / "x.txt").write_text("scratch\n")
    (repo / "pic.bin").write_bytes(b"\x00\x02")
    return repo


def _changes(c: TestClient, paths: list[str]) -> dict:
    res = c.post("/api/workspace/changes", json={"paths": paths})
    assert res.status_code == 200, res.text
    return res.json()


@needs_git
def test_changes_marks_the_chats_files_and_folds_in_the_rest(repo: Path) -> None:
    res = _changes(_client(repo), ["a.txt", "newdir/f.txt", "same-as-head.txt"])
    assert res["git"] is True and res["truncated"] is False
    rows = {f["path"]: f for f in res["files"]}
    mine = [f["path"] for f in res["files"] if f["touched"]]
    assert set(mine) == {"a.txt", "newdir/f.txt"}
    assert (
        rows["a.txt"]["status"],
        rows["a.txt"]["added"],
        rows["a.txt"]["removed"],
    ) == (
        "modified",
        2,
        1,
    )
    # Inside an untracked directory of nothing else: the fold's row would
    # only repeat it, so it goes.
    assert rows["newdir/f.txt"]["status"] == "untracked"
    assert "newdir" not in rows
    assert rows["b.txt"]["status"] == "deleted" and rows["b.txt"]["removed"] == 2
    assert rows["new.txt"]["status"] == "untracked"
    assert rows["pic.bin"]["status"] == "modified" and rows["pic.bin"]["added"] is None
    # Hidden by the agent's denial, and the scratch dir: never listed.
    assert "secrets.env" not in rows and "tmp/x.txt" not in rows
    # The chat's own lead; git's others follow in path order.
    others = [f["path"] for f in res["files"] if not f["touched"]]
    assert others == sorted(others)
    assert res["files"].index(rows["a.txt"]) < res["files"].index(rows["b.txt"])


@needs_git
def test_an_untracked_directory_unfolds_only_around_the_chats_files(
    repo: Path,
) -> None:
    (repo / "newdir" / "g.txt").write_text("g\n")  # beside the chat's f.txt
    (repo / "newdir" / "sub").mkdir()
    (repo / "newdir" / "sub" / "h.txt").write_text("h\n")
    (repo / "other").mkdir()  # holds none of the chat's files
    (repo / "other" / "x.txt").write_text("x\n")
    files = _changes(_client(repo), ["newdir/f.txt"])["files"]
    rows = {f["path"]: f for f in files}
    assert rows["newdir/f.txt"]["touched"]
    for path in ("newdir/g.txt", "newdir/sub/h.txt"):
        assert rows[path]["status"] == "untracked" and not rows[path]["touched"]
    assert "newdir" not in rows
    assert rows["other"]["is_dir"] is True  # still folded
    assert [f["path"] for f in files].count("newdir/f.txt") == 1


@needs_git
def test_a_workspace_below_the_repository_root_sees_its_own_paths(repo: Path) -> None:
    sub = repo / "sub"
    sub.mkdir()
    (sub / "s.txt").write_text("1\n")
    _git(repo, "add", "sub")
    _git(repo, "commit", "-q", "-m", "sub")
    (sub / "s.txt").write_text("2\n")
    (sub / "t.txt").write_text("t\n")
    res = _changes(_client(sub), ["s.txt"])
    assert [(f["path"], f["status"]) for f in res["files"]] == [
        ("s.txt", "modified"),
        ("t.txt", "untracked"),
    ]  # nothing from above the workspace
    diff = _client(sub).get("/api/workspace/diff", params={"path": "s.txt"}).json()
    assert "-1\n+2\n" in diff["diff"]


@needs_git
def test_diff_is_the_file_against_head(repo: Path) -> None:
    c = _client(repo)
    d = c.get("/api/workspace/diff", params={"path": "a.txt"}).json()
    assert d["binary"] is False and d["truncated"] is False
    assert "@@" in d["diff"] and "-old\n+new\n" in d["diff"] and "+more\n" in d["diff"]
    assert c.get("/api/workspace/diff", params={"path": "pic.bin"}).json() == {
        "path": "pic.bin",
        "diff": "",
        "binary": True,
        "truncated": False,
    }
    gone = c.get("/api/workspace/diff", params={"path": "b.txt"}).json()
    assert "-old\n-same\n" in gone["diff"]
    denied = c.get("/api/workspace/diff", params={"path": "secrets.env"})
    assert denied.status_code == 403
    # A path is a path: no glob reaches the denied file it doesn't name.
    glob = c.get("/api/workspace/diff", params={"path": "*.env"}).json()
    assert glob["diff"] == ""
    # Nor does a directory, whose diff would carry every file under it.
    root = c.get("/api/workspace/diff", params={"path": "."})
    assert (root.status_code, root.json()["detail"]["code"]) == (403, "path_denied")
    assert "leak" not in root.text
    # And no path is no pathspec at all: refused, not a whole-tree diff.
    assert c.get("/api/workspace/diff", params={"path": ""}).status_code == 422
    blank_old = {"path": "a.txt", "old": ""}
    assert c.get("/api/workspace/diff", params=blank_old).status_code == 200


@needs_git
def test_a_denied_file_moved_to_a_readable_name_stays_denied(repo: Path) -> None:
    """Paired as a rename, the diff of the new name would carry the denied
    file's deleted lines: both names are judged."""
    (repo / "secrets.env").write_text("".join(f"KEY{i}=v\n" for i in range(10)))
    _git(repo, "commit", "-qam", "longer secrets")
    _git(repo, "mv", "secrets.env", "public.txt")
    (repo / "public.txt").write_text("".join(f"KEY{i}=v\n" for i in range(9)))
    c = _client(repo)
    res = c.get("/api/workspace/diff", params={"path": "."})
    assert res.status_code == 403 and "KEY" not in res.text
    # By its own name the file is readable, and git doesn't pair it.
    own = c.get("/api/workspace/diff", params={"path": "public.txt"})
    assert own.status_code == 200 and "rename from" not in own.json()["diff"]


class _CutNames:
    """A session whose file list for the precheck comes back clipped."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    def decide_path(self, path: str, *, write: bool = False):  # noqa: ANN201 - test double
        return Verdict("allow")

    async def run(self, command: str, **kw) -> CommandResult:  # noqa: ANN003 - test double
        self.ran.append(command)
        if "rev-parse" in command:
            return CommandResult(exit_code=0, stdout="\n", stderr="")
        return CommandResult(exit_code=0, stdout="a.txt\0", stderr="", truncated=True)


async def test_a_clipped_file_list_refuses_the_diff() -> None:
    """What the clip dropped was never judged: no diff rather than a guess."""
    session = _CutNames()
    with pytest.raises(PermissionDeniedError):
        await file_diff(session, ".", limit=1000)  # type: ignore[arg-type]
    assert not any("--no-ext-diff" in c for c in session.ran)


@needs_git
def test_a_directory_of_readable_files_still_diffs(repo: Path) -> None:
    (repo / "secrets.env").write_text("old\nsame\n")  # back to HEAD
    (repo / "pic.bin").write_bytes(b"\x00\x01")  # (a binary marks the whole diff)
    d = _client(repo).get("/api/workspace/diff", params={"path": "."}).json()
    assert "a/a.txt" in d["diff"] and "a/b.txt" in d["diff"]


@needs_git
def test_a_rename_diffs_against_its_old_path(repo: Path) -> None:
    # Similar enough for git to call it a move (≥ 50%), with one line edited.
    body = "".join(f"line {i}\n" for i in range(10))
    (repo / "a.txt").write_text("old\n" + body)
    _git(repo, "commit", "-qam", "longer")
    _git(repo, "mv", "a.txt", "moved.txt")
    (repo / "moved.txt").write_text("new\n" + body)
    rows = {f["path"]: f for f in _changes(_client(repo), [])["files"]}
    assert (rows["moved.txt"]["status"], rows["moved.txt"]["old_path"]) == (
        "renamed",
        "a.txt",
    )
    c = _client(repo)
    d = c.get("/api/workspace/diff", params={"path": "moved.txt", "old": "a.txt"})
    assert "rename from a.txt" in d.json()["diff"]
    assert "-old\n+new\n" in d.json()["diff"]  # the edit, not a whole new file
    denied = {"path": "moved.txt", "old": "secrets.env"}
    assert c.get("/api/workspace/diff", params=denied).status_code == 403


class _Refusing:
    """A sandboxing executor that will not run git."""

    async def run(self, command, **kw):  # noqa: ANN001, ANN003 - test double
        raise WorkspaceError("sandbox: no git")


@needs_git
def test_a_git_the_executor_refuses_reads_as_no_git(repo: Path) -> None:
    res = _changes(_client(repo, executor=_Refusing()), ["a.txt"])
    assert res["git"] is False
    assert [f["path"] for f in res["files"]] == ["a.txt"]


@needs_git
def test_a_long_diff_is_cut_at_a_line(repo: Path) -> None:
    (repo / "a.txt").write_text("".join(f"line {i}\n" for i in range(200)))
    c = _client(repo, limits=WorkspaceLimits(max_shell_output_chars=300))
    d = c.get("/api/workspace/diff", params={"path": "a.txt"}).json()
    assert d["truncated"] is True
    assert len(d["diff"]) <= 300 and d["diff"].endswith("\n")


@needs_git
def test_a_repository_without_a_commit_has_only_new_files(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("x\n")
    _git(tmp_path, "init", "-q")
    c = _client(tmp_path)
    res = _changes(c, ["x.txt"])
    assert res["git"] is True
    assert [(f["path"], f["status"]) for f in res["files"]] == [("x.txt", "untracked")]
    assert c.get("/api/workspace/diff", params={"path": "x.txt"}).status_code == 404


def test_outside_git_there_is_no_diff(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("x\n")
    res = _client(tmp_path).get("/api/workspace/diff", params={"path": "x.txt"})
    assert (res.status_code, res.json()["detail"]["code"]) == (
        404,
        "feature_unavailable",
    )


@needs_git
async def test_the_git_view_runs_those_three_reads_only(repo: Path) -> None:
    session = await open_view(Workspace.local(str(repo)), git=True)
    try:
        assert (await session.run("git status --porcelain")).ok
        for command in ("git log -1", "git status; rm a.txt", "ls"):
            with pytest.raises(PermissionDeniedError):
                await session.run(command)
    finally:
        await session.close()
    assert (repo / "a.txt").exists()


# ------------------------------------------------------------- parsing -


def test_parse_status_records() -> None:
    out = "\0".join(
        [
            " M ws/a.txt",
            "R  ws/new.txt",
            "ws/old.txt",
            "?? ws/dir/",
            "D  ws/gone.txt",
            "A  ws/added.txt",
            "?? elsewhere.txt",
            "",
        ]
    )
    entries = parse_status(out, "ws/")
    assert [(e.path, e.status, e.is_dir, e.old_path) for e in entries] == [
        ("a.txt", "modified", False, None),
        ("new.txt", "renamed", False, "old.txt"),
        ("dir", "untracked", True, None),
        ("gone.txt", "deleted", False, None),
        ("added.txt", "added", False, None),
    ]


def test_parse_numstat_records() -> None:
    out = "\0".join(
        ["3\t1\ta.txt", "-\t-\tpic.bin", "2\t0\t", "old.txt", "new.txt", ""]
    )
    assert parse_numstat(out, "") == {"a.txt": (3, 1), "new.txt": (2, 0)}
