"""``GET /api/workspace/paths``: the file list behind the composer's ``@`` menu.

It is the agent's own globbed ``list_files`` run through the Files panel's
read-only view, so what it leaves out is what the agent's listing leaves out:
git-ignored files, dotfiles, ``denied_paths``, directories.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import WorkspaceSwitch, create_app  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402
from lovia.workspace import Workspace  # noqa: E402

from ..scripted_provider import ScriptedProvider, text  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


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
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)


def seed_repo(root: Path) -> None:
    """A work tree with one of each thing the list must leave out, and an
    untracked file it must keep."""
    root.mkdir(exist_ok=True)
    _git(root, "init", "-q")
    _write(
        root,
        {
            ".gitignore": "build/\n*.log\n",
            "src/app.py": "",
            "src/my file.py": "",
            "README.md": "",
            ".github/workflows/ci.yml": "",
            "secrets.env": "KEY=1",
        },
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    _write(root, {"src/new.py": "", "build/out.js": "", "debug.log": ""})


def _agent(root: Path) -> Agent:
    return Agent(
        name="bot",
        model=ScriptedProvider([text("ok")] * 4),
        workspace=Workspace.local(str(root), denied_paths=("secrets.env",)),
    )


def _client(agent: Agent, switch: WorkspaceSwitch | None = None) -> TestClient:
    app = create_app(
        agent, store=ChatStore.in_memory(), titles=False, workspaces=switch
    )
    return TestClient(app)


EXPECTED = ["README.md", "src/app.py", "src/my file.py", "src/new.py"]


@needs_git
def test_lists_the_files_the_agent_would_list(tmp_path: Path) -> None:
    seed_repo(tmp_path / "repo")
    body = _client(_agent(tmp_path / "repo")).get("/api/workspace/paths").json()
    # Ignored (build/, *.log), hidden (.gitignore, .github/), denied
    # (secrets.env) and directories (src) are out; untracked src/new.py is in.
    assert body == {"paths": EXPECTED, "truncated": False}


def test_outside_git_it_still_lists_files_only(tmp_path: Path) -> None:
    _write(tmp_path, {"a/b.txt": "", "c.txt": "", ".hidden": "", "secrets.env": ""})
    body = _client(_agent(tmp_path)).get("/api/workspace/paths").json()
    assert body == {"paths": ["a/b.txt", "c.txt"], "truncated": False}


@needs_git
def test_says_when_the_cap_cut_it(tmp_path: Path, monkeypatch) -> None:
    from lovia.web.api import workspace as routes

    seed_repo(tmp_path / "repo")
    monkeypatch.setattr(routes, "_PATHS_CAP", 2)
    body = _client(_agent(tmp_path / "repo")).get("/api/workspace/paths").json()
    assert body["truncated"] is True
    # The cap counts directories too: "README.md", "src" → one file.
    assert body["paths"] == ["README.md"]


@needs_git
def test_a_draft_and_a_chat_list_their_own_directory(tmp_path: Path) -> None:
    launch, other = tmp_path / "launch", tmp_path / "other"
    launch.mkdir()
    (launch / "launch-only.txt").write_text("x")
    seed_repo(other)
    with _client(_agent(launch), WorkspaceSwitch()) as c:
        assert c.get("/api/workspace/paths").json()["paths"] == ["launch-only.txt"]
        draft = c.get("/api/workspace/paths", params={"root": str(other)})
        assert draft.json()["paths"] == EXPECTED
        sid = c.post(
            "/api/chat", json={"message": "go", "workspace": str(other)}
        ).json()["session_id"]
        chat = c.get("/api/workspace/paths", params={"session": sid})
        assert chat.json()["paths"] == EXPECTED
        # A file made after the first listing is in the next one.
        (other / "src" / "later.py").write_text("")
        again = c.get("/api/workspace/paths", params={"session": sid}).json()
        assert "src/later.py" in again["paths"]


def test_no_workspace_no_list(tmp_path: Path) -> None:
    plain = Agent(name="plain", model=ScriptedProvider([text("hi")]))
    res = _client(plain).get("/api/workspace/paths")
    assert (res.status_code, res.json()["detail"]["code"]) == (
        404,
        "feature_unavailable",
    )
