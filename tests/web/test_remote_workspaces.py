"""Chats that work on a remote host: ``WorkspaceSwitch(remotes=...)``.

The "remote" host is a bare subprocess of this interpreter (``-I -S``), so a
chat's tools run through the real remote machinery — into a directory that
merely happens to be on the same disk, which is how the tests can look.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import WorkspaceSwitch, create_app  # noqa: E402
from lovia.web.builder import Deployment  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402
from lovia.web.supervisor import agent_grant_context  # noqa: E402
from lovia.web.workspaces import ChatBinding, WorkspaceRef  # noqa: E402
from lovia.workspace import RemoteHost, RemoteWorkspace, Workspace  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX far side")

_WRITE = call("write_file", {"path": "out.txt", "content": "hi"})


def _far() -> RemoteHost:
    return RemoteHost("far", command=(), python=f"{sys.executable} -I -S")


def _agent(root: Path, script: list) -> Agent:
    return Agent(
        name="bot",
        model=ScriptedProvider(script),
        workspace=Workspace.local(str(root)),
    )


def _client(agent: Agent, switch: WorkspaceSwitch | None) -> TestClient:
    app = create_app(
        agent, store=ChatStore.in_memory(), titles=False, workspaces=switch
    )
    return TestClient(app)


@pytest.fixture
def places(tmp_path: Path) -> tuple[Path, Path]:
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    return here, there.resolve()


# ------------------------------------------------------------- the key --


def test_a_location_is_one_string_that_reads_back() -> None:
    for key, scheme, host, root, name in (
        ("/home/me/project", None, None, "/home/me/project", None),
        ("ssh://box/srv/project", "ssh", "box", "/srv/project", "ssh:box"),
        ("ssh://neo@10.0.0.2/srv/a b", "ssh", "neo@10.0.0.2", "/srv/a b", "ssh:neo@10.0.0.2"),
        ("docker://dev@api/app", "docker", "dev@api", "/app", "docker:dev@api"),
        ("remote://far/~/project", "remote", "far", "~/project", "far"),
        # Looks a little like one, is a local directory.
        ("/tmp/odd://name", None, None, "/tmp/odd://name", None),
        ("git://host/x", None, None, "git://host/x", None),
    ):  # fmt: skip
        ref = WorkspaceRef.parse(key)
        assert (ref.scheme, ref.host, ref.root, ref.host_name) == (
            scheme,
            host,
            root,
            name,
        )
        assert ref.key == key and ref.remote is (scheme is not None)


def test_any_remote_builds_one_host_per_destination() -> None:
    switch = WorkspaceSwitch(remotes="any")
    box = switch.remote_host(WorkspaceRef.parse("ssh://box/srv/a"))
    assert box is switch.remote_host(WorkspaceRef.parse("ssh://box/srv/b"))
    assert box.name == "ssh:box" and box.command[0] == "ssh"
    api = switch.remote_host(WorkspaceRef.parse("docker://dev@api/app"))
    assert api.name == "docker:dev@api"
    assert list(api.command) == ["docker", "exec", "-i", "-u", "dev", "api"]
    # Declared hosts only answer to their names.
    declared = WorkspaceSwitch(remotes={"far": box})
    assert declared.remote_refusal(WorkspaceRef.parse("remote://far/x")) is None
    assert "no remote host named" in (
        declared.remote_refusal(WorkspaceRef.parse("remote://other/x")) or ""
    )
    assert declared.remote_refusal(WorkspaceRef.parse("ssh://box/x")) is not None
    assert WorkspaceSwitch().remote_refusal(WorkspaceRef.parse("ssh://box/x"))


def test_lovia_web_opens_remotes_on_loopback_only() -> None:
    assert Deployment(workspace="/w").remotes() == "any"
    assert Deployment(workspace="/w", lock_workspace=True).remotes() is None
    assert Deployment(workspace=None).remotes() is None
    exposed = Deployment(workspace="/w", loopback=False, allow_workspace_edit=True)
    # --allow-workspace-switch opens this server's directories, not its keys.
    assert exposed.roots() == "any" and exposed.remotes() is None


# ----------------------------------------------------------- end to end --


def test_a_chat_works_at_the_remote_location_it_was_opened_in(places) -> None:
    here, there = places
    far = _far()
    agent = _agent(here, [_WRITE, text("one"), _WRITE, text("two")])
    key = f"remote://far{there}"
    with _client(agent, WorkspaceSwitch(remotes={"far": far})) as c:
        try:
            res = c.post("/api/chat", json={"message": "go", "workspace": key})
            assert res.status_code == 200, res.text
            sid = res.json()["session_id"]
            # The tool ran over there, not in the agent's own directory.
            assert (there / "out.txt").read_text() == "hi"
            assert not (here / "out.txt").exists()
            assert c.get(f"/api/sessions/{sid}").json()["workspace_root"] == key

            # The chat's next run is in the same place, on the same session.
            (there / "out.txt").unlink()
            c.post("/api/chat", json={"message": "again", "session_id": sid})
            assert (there / "out.txt").read_text() == "hi"

            # It is listed, by its host; the local one is not.
            listed = c.get("/api/workspaces").json()
            assert listed["remotes"] == ["far"]
            by_root = {w["root"]: w for w in listed["workspaces"]}
            assert by_root[key]["host"] == "far" and by_root[key]["name"] == "there"
            assert by_root[here.resolve().as_posix()]["host"] is None

            # The Files panel reads this machine's disk: not for this chat.
            files = c.get("/api/workspace/files", params={"session": sid})
            assert files.status_code == 404
            assert files.json()["detail"]["code"] == "feature_unavailable"
        finally:
            c.portal.call(far.aclose)


def test_the_chat_mode_is_enforced_on_the_host(places) -> None:
    here, there = places
    far = _far()
    agent = _agent(here, [_WRITE, text("done")])
    with _client(agent, WorkspaceSwitch(remotes={"far": far})) as c:
        try:
            res = c.post(
                "/api/chat",
                json={
                    "message": "go",
                    "workspace": f"remote://far{there}",
                    "workspace_mode": "readonly",
                },
            )
            assert res.status_code == 200, res.text
            assert not (there / "out.txt").exists()
        finally:
            c.portal.call(far.aclose)


def test_adding_a_remote_location_looks_it_up_on_its_host(places) -> None:
    here, there = places
    (there.parent / "link").symlink_to(there)
    far = _far()
    with _client(_agent(here, [text("x")]), WorkspaceSwitch(remotes={"far": far})) as c:
        try:
            added = c.post(
                "/api/workspaces", json={"path": f"remote://far{there.parent}/link"}
            )
            assert added.status_code == 200, added.text
            # Stored by where it resolves to there: one directory, one entry.
            assert added.json()["root"] == f"remote://far{there}"
            assert added.json()["host"] == "far"

            missing = c.post(
                "/api/workspaces", json={"path": f"remote://far{there}/nope"}
            )
            assert missing.status_code == 404
            assert missing.json()["detail"]["code"] == "file_not_found"
        finally:
            c.portal.call(far.aclose)


def test_remote_locations_are_refused_where_they_are_not_offered(places) -> None:
    here, there = places
    key = f"remote://far{there}"
    # No remotes at all (the default), and a host that was never declared.
    for switch, said in (
        (WorkspaceSwitch(), "doesn't open remote workspaces"),
        (WorkspaceSwitch(remotes={"other": _far()}), "no remote host named 'far'"),
    ):
        with _client(_agent(here, [text("x")]), switch) as c:
            res = c.post("/api/chat", json={"message": "go", "workspace": key})
            assert res.status_code == 409, res.text
            error = res.json()["detail"]
            assert error["code"] == "workspace_not_allowed" and said in error["hint"]
    # A destination that is not one safe word never reaches a command line.
    with _client(_agent(here, [text("x")]), WorkspaceSwitch(remotes="any")) as c:
        res = c.post(
            "/api/chat",
            json={"message": "go", "workspace": "ssh://-oProxyCommand=evil/srv"},
        )
        assert res.status_code == 400, res.text
        assert res.json()["detail"]["code"] == "invalid_request"


def test_an_approval_is_remembered_for_the_host_and_directory(places) -> None:
    here, there = places
    far = _far()
    agent = _agent(here, [text("x")])
    with _client(agent, WorkspaceSwitch(remotes={"far": far})) as c:
        deps = c.app.state.deps
        at = deps.at_root(agent, ChatBinding(f"remote://far{there}", None))
        assert isinstance(at.workspace, RemoteWorkspace)
        assert agent_grant_context("bot", at) == (
            f'["bot", "{there.as_posix()}", "far"]'
        )
        # Not the local directory of the same path.
        local = deps.at_root(agent, ChatBinding(there.as_posix(), None))
        assert agent_grant_context("bot", local) != agent_grant_context("bot", at)
