"""Chats that work on a remote host: ``WorkspaceSwitch(remotes=...)``.

The "remote" host is a bare subprocess of this interpreter (``-I -S``), so a
chat's tools run through the real remote machinery — into a directory that
merely happens to be on the same disk, which is how the tests can look.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.parts import ImagePart  # noqa: E402
from lovia.run_context import RunContext  # noqa: E402
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


def test_lovia_web_opens_remotes_wherever_chats_pick_a_directory() -> None:
    # Whatever the bind address: only the explicit flags close it.
    assert Deployment(workspace="/w").remotes() == "any"
    assert Deployment(workspace="/w", readonly=True).remotes() == "any"
    assert Deployment(workspace="/w", lock_workspace=True).remotes() is None
    assert Deployment(workspace=None).remotes() is None


# ----------------------------------------------------------- end to end --


def test_a_chat_works_at_the_remote_location_it_was_opened_in(places) -> None:
    here, there = places
    far = _far()
    rewrite = call("write_file", {"path": "out.txt", "content": "again"})
    agent = _agent(here, [_WRITE, text("one"), rewrite, text("two")])
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
            c.post("/api/chat", json={"message": "again", "session_id": sid})
            assert (there / "out.txt").read_text() == "again"

            # It is listed, by its host; the local one is not.
            listed = c.get("/api/workspaces").json()
            assert listed["remotes"] == ["far"]
            by_root = {w["root"]: w for w in listed["workspaces"]}
            assert by_root[key]["host"] == "far" and by_root[key]["name"] == "there"
            assert by_root[here.resolve().as_posix()]["host"] is None

        finally:
            c.portal.call(far.aclose)


# ------------------------------------------- Files, uploads, attachments --

_PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


@pytest.fixture
def remote_chat(places):
    """A client, a chat at the remote location (one run done), and the
    directory it works in — for the tests to look at from this side."""
    here, there = places
    far = _far()
    agent = _agent(here, [text("ok")] * 8)
    agent.model.supports_vision = True  # type: ignore[union-attr]
    switch = WorkspaceSwitch(remotes={"far": far}, modes=("readonly", "coding"))
    with _client(agent, switch) as c:
        try:
            res = c.post(
                "/api/chat", json={"message": "go", "workspace": f"remote://far{there}"}
            )
            assert res.status_code == 200, res.text
            yield c, res.json()["session_id"], there
        finally:
            c.portal.call(far.aclose)


def test_the_files_panel_reads_a_remote_chats_directory(remote_chat, places) -> None:
    c, sid, there = remote_chat
    (there / "docs").mkdir()
    (there / "docs" / "note.md").write_text("# hi\nthere\n")
    (there / "tmp").mkdir()
    (there / "tmp" / "scratch.txt").write_text("x")
    (there / ".env").write_text("SECRET=1")
    (places[0] / "local-only.txt").write_text("not over there")
    at = {"session": sid}

    assert c.get("/api/workspace", params=at).json()["name"] == "there"
    listed = c.get("/api/workspace/files", params=at).json()
    assert listed["truncated"] is False  # the flag rides the wire (ClippedList)
    assert [(e["path"], e["is_dir"]) for e in listed["files"]] == [
        ("docs", True),
        ("tmp", True),
    ]
    docs = c.get("/api/workspace/files", params={**at, "path": "docs"})
    inside = docs.json()["files"]
    assert [e["path"] for e in inside] == ["docs/note.md"]
    assert inside[0]["size"] == 11 and inside[0]["mtime"]
    # The change list: stat-ed and judged over there, git asked over there.
    paths = ["docs/note.md", "tmp/scratch.txt", ".env", "local-only.txt"]
    changes = c.post("/api/workspace/changes", params=at, json={"paths": paths})
    assert changes.json()["git"] is False
    assert [e["path"] for e in changes.json()["files"]] == ["docs/note.md"]

    page = c.get("/api/workspace/file", params={**at, "path": "docs/note.md"}).json()
    assert (page["content"], page["total_lines"]) == ("# hi\nthere\n", 2)
    assert not page["binary"]

    # The same boundary as a local chat's panel, judged on the host.
    for path, status, code in (
        (".env", 403, "path_denied"),
        ("/etc/hosts", 403, "path_denied"),
        ("../here/local-only.txt", 403, "path_denied"),
        ("nope.md", 404, "file_not_found"),
        ("docs", 404, "file_not_found"),
    ):
        for route in ("file", "raw"):
            res = c.get(
                f"/api/workspace/{route}", params={**at, "path": path, "download": 1}
            )
            assert (res.status_code, res.json()["detail"]["code"]) == (status, code)
    missing = c.get("/api/workspace/files", params={**at, "path": "nope"})
    assert missing.status_code == 404


def test_a_draft_reads_the_remote_folder_it_is_about_to_open(remote_chat) -> None:
    c, _sid, there = remote_chat
    (there / "a.txt").write_text("a")
    at = {"root": f"remote://far{there}"}
    listed = c.get("/api/workspace/files", params=at).json()["files"]
    assert [e["path"] for e in listed] == ["a.txt"]
    # … and only a remote this server opens.
    res = c.get("/api/workspace/files", params={"root": f"remote://other{there}"})
    assert (res.status_code, res.json()["detail"]["code"]) == (
        409,
        "workspace_not_allowed",
    )


def test_raw_serves_a_remote_file_in_pieces(remote_chat, monkeypatch) -> None:
    from lovia.web.api import workspace as routes

    c, sid, there = remote_chat
    monkeypatch.setattr(routes, "_RAW_CHUNK", 1000)  # several pieces, small file
    blob = bytes(range(256)) * 20  # 5120 bytes: six pieces
    (there / "data.bin").write_bytes(blob)
    (there / "pic.png").write_bytes(_PNG)
    (there / "报告 1.txt").write_text("text")
    at = {"session": sid}

    got = c.get("/api/workspace/raw", params={**at, "path": "data.bin", "download": 1})
    assert got.status_code == 200 and got.content == blob
    assert got.headers["content-length"] == str(len(blob))
    assert got.headers["content-disposition"] == 'attachment; filename="data.bin"'
    assert got.headers["x-content-type-options"] == "nosniff"
    # Revalidation costs a look at the file, not the file.
    again = c.get(
        "/api/workspace/raw",
        params={**at, "path": "data.bin", "download": 1},
        headers={"if-none-match": got.headers["etag"]},
    )
    assert again.status_code == 304 and again.content == b""
    (there / "data.bin").write_bytes(blob + b"more")
    changed = c.get(
        "/api/workspace/raw",
        params={**at, "path": "data.bin", "download": 1},
        headers={"if-none-match": got.headers["etag"]},
    )
    assert changed.status_code == 200 and changed.content == blob + b"more"

    # Inline: images (and PDFs) only, with the stable type.
    pic = c.get("/api/workspace/raw", params={**at, "path": "pic.png"})
    assert pic.content == _PNG and pic.headers["content-type"] == "image/png"
    assert "content-disposition" not in pic.headers
    refused = c.get("/api/workspace/raw", params={**at, "path": "data.bin"})
    assert refused.status_code == 415
    named = c.get(
        "/api/workspace/raw", params={**at, "path": "报告 1.txt", "download": 1}
    )
    assert named.headers["content-disposition"] == (
        "attachment; filename*=utf-8''%E6%8A%A5%E5%91%8A%201.txt"
    )
    # The binary flag for the viewer comes from the file's head, read there.
    flagged = c.get("/api/workspace/file", params={**at, "path": "data.bin"}).json()
    assert flagged["binary"] and flagged["content"] == ""


def test_raw_refuses_a_remote_file_over_the_read_limit(places) -> None:
    from lovia.workspace import WorkspaceLimits

    here, there = places
    far = _far()
    (there / "big.bin").write_bytes(b"x" * 64)
    agent = Agent(
        name="bot",
        model=ScriptedProvider([text("ok")]),
        workspace=Workspace.local(
            str(here), limits=WorkspaceLimits(max_file_read_bytes=32)
        ),
    )
    with _client(agent, WorkspaceSwitch(remotes={"far": far})) as c:
        try:
            res = c.get(
                "/api/workspace/raw",
                params={
                    "root": f"remote://far{there}",
                    "path": "big.bin",
                    "download": 1,
                },
            )
            assert (res.status_code, res.json()["detail"]["code"]) == (
                413,
                "file_too_large",
            )
        finally:
            c.portal.call(far.aclose)


@pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="needs a directory the account cannot write to",
)
def test_a_write_the_host_refuses_is_not_a_missing_file(remote_chat) -> None:
    # The host's own "no" (here: no permission on uploads/) comes back as
    # what it is, with what the system said — not as 404 file_not_found.
    c, sid, there = remote_chat
    (there / "uploads").mkdir()
    (there / "uploads").chmod(0o555)
    try:
        res = c.post(
            "/api/workspace/upload",
            params={"session": sid},
            files={"file": ("a.txt", b"data")},
        )
    finally:
        (there / "uploads").chmod(0o755)
    detail = res.json()["detail"]
    assert (res.status_code, detail["code"]) == (403, "path_denied"), res.text
    assert "Permission denied" in detail["message"]


def test_an_upload_lands_in_the_remote_chats_directory(remote_chat) -> None:
    c, sid, there = remote_chat
    # The chat is read-only; an upload is the user's own act and still lands.
    assert c.patch(
        f"/api/sessions/{sid}", json={"workspace_mode": "readonly"}
    ).is_success
    at = {"session": sid}

    def upload(name: str, data: bytes):
        return c.post("/api/workspace/upload", params=at, files={"file": (name, data)})

    first = upload("cat.png", _PNG)
    assert first.status_code == 200, first.text
    assert first.json() == {
        "path": "uploads/cat.png",
        "name": "cat.png",
        "mime": "image/png",
        "kind": "image",
        "size": len(_PNG),
    }
    assert (there / "uploads" / "cat.png").read_bytes() == _PNG
    # A second one of the same name is kept beside it, not over it.
    assert upload("cat.png", b"other").json()["path"] == "uploads/cat-1.png"
    assert (there / "uploads" / "cat.png").read_bytes() == _PNG
    assert (there / "uploads" / "cat-1.png").read_bytes() == b"other"
    # It is there for the panel at once.
    shown = c.get("/api/workspace/raw", params={**at, "path": "uploads/cat.png"})
    assert shown.content == _PNG


def test_attachments_reach_a_remote_chat(remote_chat) -> None:
    c, sid, there = remote_chat
    (there / "uploads").mkdir()
    (there / "uploads" / "cat.png").write_bytes(_PNG)
    (there / "uploads" / "notes.txt").write_text("n")
    res = c.post(
        "/api/chat",
        json={
            "message": "look",
            "session_id": sid,
            "attachments": [
                {"path": "uploads/cat.png", "mime": "image/png", "kind": "image"},
                {"path": "uploads/notes.txt", "mime": "text/plain", "kind": "file"},
                {"path": "uploads/ghost.png", "mime": "image/png", "kind": "image"},
                {"path": "/etc/hosts", "mime": "text/plain", "kind": "file"},
            ],
        },
    )
    assert res.status_code == 200, res.text
    sent = c.app.state.deps.agents["bot"].model.calls[-1][-1]
    assert sent.role == "user"
    images = [p for p in sent.content if isinstance(p, ImagePart)]
    assert [base64.b64decode(p.data) for p in images] == [_PNG]
    note = sent.content[-1].text
    # What is there is named; what is missing or outside the root is dropped.
    assert note.endswith(
        "[Attached in the workspace: uploads/cat.png, uploads/notes.txt. "
        "Read with your file tools when relevant.]"
    )


async def test_every_way_in_reads_attachments_where_the_chat_works(places) -> None:
    # Send, stream and inject all fold attachments in with the same function.
    # (Inject hands it the live run's agent: its workspace bound to the
    # chat's session.)
    from lovia.web.attachments import user_content
    from lovia.web.schemas import Attachment
    from lovia.workspace.workspace import WorkspaceSessionBinding

    _here, there = places
    (there / "a.txt").write_text("a")
    far = _far()
    remote = Workspace.remote(far, str(there))
    attachment = Attachment(path="a.txt", mime="text/plain", kind="file")
    try:
        for workspace in (remote, WorkspaceSessionBinding(remote, None)):  # type: ignore[arg-type]
            agent = Agent(name="bot", model=None, workspace=workspace)
            content = await user_content("see", [attachment], agent)
            assert [p.text for p in content] == [  # type: ignore[union-attr]
                "see",
                "\n\n[Attached in the workspace: a.txt. "
                "Read with your file tools when relevant.]",
            ]
            assert await user_content("just text", None, agent) == "just text"
    finally:
        await far.aclose()


def test_a_host_that_cannot_be_reached_is_said_so_not_dropped(places) -> None:
    here, there = places
    gone = RemoteHost("gone", command=(), python="/nonexistent/python3")
    key = f"remote://gone{there}"
    with _client(
        _agent(here, [text("x")]), WorkspaceSwitch(remotes={"gone": gone})
    ) as c:
        res = c.post("/api/workspace/changes", params={"root": key}, json={})
        assert (res.status_code, res.json()["detail"]["code"]) == (
            502,
            "workspace_unreachable",
        )
        up = c.post(
            "/api/workspace/upload",
            params={"root": key},
            files={"file": ("a.txt", b"a")},
        )
        assert up.status_code == 502
        # A message is not sent without what it was shown.
        sent = c.post(
            "/api/chat",
            json={
                "message": "look",
                "workspace": key,
                "attachments": [
                    {"path": "a.png", "mime": "image/png", "kind": "image"}
                ],
            },
        )
        assert (sent.status_code, sent.json()["detail"]["code"]) == (
            502,
            "workspace_unreachable",
        )
        assert c.get("/api/sessions").json() == []


def test_an_agent_whose_own_workspace_is_remote_has_a_files_panel(places) -> None:
    # No switch: the served agent's workspace is itself on a host.
    _here, there = places
    (there / "a.txt").write_text("a")
    far = _far()
    agent = Agent(
        name="bot",
        model=ScriptedProvider([text("ok")]),
        workspace=Workspace.remote(far, str(there), mode="readonly"),
    )
    with _client(agent, None) as c:
        try:
            assert c.get("/api/agents").json()[0]["workspace"] is True
            listed = c.get("/api/workspace/files").json()["files"]
            assert [e["path"] for e in listed] == ["a.txt"]
            up = c.post("/api/workspace/upload", files={"file": ("b.txt", b"b")})
            assert up.json()["path"] == "uploads/b.txt"
            assert (there / "uploads" / "b.txt").read_bytes() == b"b"
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


def test_the_folder_picker_lists_the_hosts_folders(places) -> None:
    here, there = places
    for name in ("alpha", "beta", ".hidden"):
        (there / name).mkdir()
    (there / "a-file.txt").write_text("x", encoding="utf-8")
    far = _far()
    with _client(_agent(here, [text("x")]), WorkspaceSwitch(remotes={"far": far})) as c:
        try:

            def dirs(path: str, host: str | None = "remote://far") -> list[str]:
                params = {"path": path, **({"host": host} if host else {})}
                res = c.get("/api/workspaces/dirs", params=params)
                assert res.status_code == 200, res.text
                return res.json()

            # As typed, folders only, hidden ones once a dot is typed.
            assert dirs(f"{there}/") == [f"{there}/alpha", f"{there}/beta"]
            assert dirs(f"{there}/al") == [f"{there}/alpha"]
            assert dirs(f"{there}/.") == [f"{there}/.hidden"]
            # A host this server doesn't open, or no host at all in the
            # value, suggests nothing rather than failing the picker.
            assert dirs(f"{there}/", host="remote://elsewhere") == []
            assert dirs(f"{there}/", host="nonsense") == []
            # Without a host it is this machine's folders, as before.
            assert dirs(f"{there}/", host=None) == [f"{there}/alpha", f"{there}/beta"]
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


# ------------------------------------------------ what rides along, or not --


async def test_a_task_spawned_in_a_remote_chat_stays_there(places) -> None:
    # A self-cloned subagent is the running agent — by then on a remote
    # workspace. Its task chat must still record where and how it works, or
    # carrying on in it afterwards has nowhere to run.
    import asyncio

    from lovia import Subagents

    here, there = places
    far = _far()
    key = f"remote://far{there}"
    parent = Agent(
        name="bot",
        model=ScriptedProvider(
            [call("spawn_subagent", {"prompt": "dig"}), text("ok"), text("dug")]
        ),
        plugins=[Subagents()],
        workspace=Workspace.local(str(here)),
    )
    app = create_app(
        parent,
        store=ChatStore.in_memory(),
        titles=False,
        workspaces=WorkspaceSwitch(remotes={"far": far}),
    )
    deps = app.state.deps
    try:
        await deps.store.upsert(
            "p", agent="bot", workspace_root=key, workspace_mode="readonly"
        )
        await deps.supervisor.start(
            session_id="p",
            agent=parent,
            input="go",
            is_new=False,
            title_message=None,
            autostart=True,
        )

        async def task_done() -> bool:
            kids = await deps.store.list_children("p")
            if len(kids) != 1:
                return False
            run = await deps.store.latest_run_for(f"subagent:{kids[0].id}")
            return run is not None and run.status != "running"

        async def wait() -> None:
            while not await task_done():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 10)
        (task,) = await deps.store.list_children("p")
        assert (task.workspace_root, task.workspace_mode) == (key, "readonly")
        # And the chat can be carried on: it resolves to the same place.
        binding = deps.chat_binding(parent, task)
        assert (binding.root, binding.mode) == (key, "readonly")
    finally:
        await deps.supervisor.shutdown()
        await deps.workspaces.aclose()
        await far.aclose()


def test_a_remote_chat_gets_the_remote_hook_not_the_local_one(places) -> None:
    here, there = places
    far = _far()
    seen: list[str] = []

    def local_hook(agent: Agent, root: Path) -> Agent:
        seen.append(f"local:{root}")
        return agent.clone(instructions="LOCAL PROJECT RULES")

    def remote_hook(agent: Agent, ref: WorkspaceRef) -> Agent:
        seen.append(f"remote:{ref.host_name}:{ref.root}")
        return agent.clone(instructions="REMOTE RULES")

    provider = ScriptedProvider([text("x")])
    agent = Agent(
        name="bot",
        model=provider,
        instructions="BASE",
        workspace=Workspace.local(str(here)),
    )
    switch = WorkspaceSwitch(
        remotes={"far": far}, agent_for=local_hook, agent_for_remote=remote_hook
    )
    with _client(agent, switch) as c:
        try:
            res = c.post(
                "/api/chat",
                json={"message": "go", "workspace": f"remote://far{there}"},
            )
            assert res.status_code == 200, res.text
        finally:
            c.portal.call(far.aclose)
    assert seen == [f"remote:far:{there}"]
    prompt = str(provider.calls[0][0])
    assert "REMOTE RULES" in prompt and "LOCAL PROJECT RULES" not in prompt


async def test_lovia_web_keeps_this_machines_project_out_of_a_remote_chat(
    tmp_path: Path,
) -> None:
    # The served agent is built for the launch directory: its AGENTS.md is in
    # the prompt, its skills are loaded. Neither belongs to a project that
    # lives on another machine.
    from lovia.plugins import Skills
    from lovia.web.builder import (
        GENERIC_INSTRUCTIONS,
        project_agent,
        remote_agent,
    )
    from lovia.web.config.schema import WebConfig

    launch = tmp_path / "launch"
    (launch / ".agents" / "skills" / "deploy").mkdir(parents=True)
    (launch / ".agents" / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: ship it\n---\nsteps", encoding="utf-8"
    )
    (launch / "AGENTS.md").write_text("LAUNCH DIRECTORY RULES", encoding="utf-8")
    config = WebConfig.model_validate({"skills": {"dirs": []}})
    spec = config.default_agent
    served = project_agent(Agent(name="bot", model=None), launch, config, spec)
    assert "LAUNCH DIRECTORY RULES" in served.instructions
    assert any(isinstance(p, Skills) for p in served.plugins)

    # No workspace to read a project file from: what is left is not the
    # launch directory's.
    nowhere = RunContext(deps=None, entries=[], agent=served)
    remote = remote_agent(served, config, spec)
    assert await remote.render_system_prompt(nowhere) == GENERIC_INSTRUCTIONS
    assert not any(isinstance(p, Skills) for p in remote.plugins)
    # The global skill directories are this server's own: they still load.
    shared = tmp_path / "shared-skills"
    shared.mkdir()
    everywhere = WebConfig.model_validate({"skills": {"dirs": [str(shared)]}})
    (skills,) = [
        p
        for p in remote_agent(served, everywhere, spec).plugins
        if isinstance(p, Skills)
    ]
    assert skills is not None
    # The agent's own prompt still applies everywhere.
    terse = remote_agent(
        served, config, spec.model_copy(update={"instructions": "be terse"})
    )
    assert await terse.render_system_prompt(nowhere) == "be terse"


def test_a_remote_chat_reads_its_own_projects_instructions(places) -> None:
    # The project lives on the host, and so does its AGENTS.md: it is read
    # over there, once per run, and this machine's is not used.
    from lovia.web.builder import remote_agent
    from lovia.web.config.schema import WebConfig

    here, there = places
    (here / "AGENTS.md").write_text("LOCAL PROJECT RULES", encoding="utf-8")
    (there / "AGENTS.md").write_text("REMOTE PROJECT RULES", encoding="utf-8")
    config = WebConfig.model_validate(
        {"agents": [{"id": "bot", "instructions": "be terse"}]}
    )
    far = _far()
    provider = ScriptedProvider(
        [call("read_file", {"path": "AGENTS.md"}), text("x"), text("y")]
    )
    agent = Agent(name="bot", model=provider, workspace=Workspace.local(str(here)))
    switch = WorkspaceSwitch(
        remotes={"far": far},
        agent_for_remote=lambda agent, ref: remote_agent(
            agent, config, config.default_agent
        ),
    )
    with _client(agent, switch) as c:
        try:
            res = c.post(
                "/api/chat",
                json={"message": "go", "workspace": f"remote://far{there}"},
            )
            assert res.status_code == 200, res.text
            # Edited between runs: the next run sees the new text, while both
            # calls of the first run saw the same one.
            (there / "AGENTS.md").write_text("NEWER RULES", encoding="utf-8")
            again = c.post(
                "/api/chat",
                json={"message": "more", "session_id": res.json()["session_id"]},
            )
            assert again.status_code == 200, again.text
        finally:
            c.portal.call(far.aclose)
    first, second, third = (str(sent[0]) for sent in provider.calls)
    assert "be terse" in first and "REMOTE PROJECT RULES" in first
    assert "LOCAL PROJECT RULES" not in first
    assert second == first
    assert "NEWER RULES" in third and "REMOTE PROJECT RULES" not in third


async def test_a_remote_project_file_that_cannot_be_read_is_left_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Hidden by the chat's policy, or far too large to be instructions: the
    # run goes on with the server-wide prompt alone.
    from lovia.web.builder import GENERIC_INSTRUCTIONS, remote_instructions

    (tmp_path / "AGENTS.md").write_text("x" * 300_000, encoding="utf-8")
    for workspace in (
        Workspace.local(str(tmp_path), mode="readonly"),
        Workspace.local(str(tmp_path), mode="readonly", denied_paths=("AGENTS.md",)),
    ):
        session = await workspace.open()
        try:
            ctx = RunContext(
                deps=None, entries=[], agent=Agent(name="bot"), workspace=session
            )
            assert await remote_instructions()(ctx) == GENERIC_INSTRUCTIONS
        finally:
            await session.close()
    assert "AGENTS.md: instructions are limited to 262,144 bytes" in caplog.text
    assert "AGENTS.md: a workspace rule covers it" in caplog.text


async def test_a_remote_chat_in_a_subdirectory_gets_what_a_local_one_does(
    tmp_path: Path,
) -> None:
    # Read-only denies reads outside the root; the repository's own rules
    # above the chat directory load all the same, here and on a host.
    from lovia.web.builder import remote_instructions, resolve_instructions

    repo = tmp_path.resolve() / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "pkg").mkdir()
    (repo / "AGENTS.md").write_text("repo rules", encoding="utf-8")
    (repo / "pkg" / "AGENTS.md").write_text("pkg rules", encoding="utf-8")
    workspace = Workspace.local(str(repo / "pkg"), mode="readonly")
    session = await workspace.open()
    try:
        ctx = RunContext(
            deps=None, entries=[], agent=Agent(name="bot"), workspace=session
        )
        got = await remote_instructions("be terse")(ctx)
    finally:
        await session.close()
    assert got == resolve_instructions("be terse", repo / "pkg", workspace.policy)
    assert "repo rules" in got and "pkg rules" in got


def test_a_sandboxed_agent_is_not_quietly_unsandboxed_on_a_remote(places) -> None:
    # A shell executor confines commands on this machine. It cannot go with
    # the workspace to a host, and running there without it would be a
    # weaker boundary nobody asked for.
    from lovia.exceptions import UserError

    here, there = places

    class Sandbox:
        async def run(self, command, *, cwd, env, timeout, policy, root):
            raise AssertionError("never runs here")

    agent = Agent(
        name="bot",
        model=ScriptedProvider([_WRITE, text("x")]),
        workspace=Workspace.local(str(here), executor=Sandbox()),
    )
    key = f"remote://far{there}"
    switch = WorkspaceSwitch(remotes={"far": _far()})
    with _client(agent, switch) as c:
        res = c.post("/api/chat", json={"message": "go", "workspace": key})
        assert res.status_code == 409, res.text
        detail = res.json()["detail"]
        assert detail["code"] == "workspace_not_allowed"
        assert "executor" in detail["hint"]
        assert c.get("/api/sessions").json() == []
    assert not (there / "out.txt").exists()
    # The builder itself refuses too, whatever path leads to it.
    with pytest.raises(UserError, match="executor"):
        switch.remote_workspace(agent, WorkspaceRef.parse(key), None)


def test_a_sandbox_that_comes_with_a_mode_is_caught_for_that_mode(places) -> None:
    # The agent's own workspace has no executor; the switch's ``build`` adds
    # one to the modes it builds. Whether a remote chat can be given its
    # workspace then depends on the mode — and must be settled before the
    # chat is written, as a refusal, not found out as a failed start.
    here, there = places
    far = _far()

    class Sandbox:
        async def run(self, command, *, cwd, env, timeout, policy, root):
            raise AssertionError("never runs here")

    def build(agent: Agent, root: Path, mode: str):
        return Workspace.local(str(root), mode=mode, executor=Sandbox())  # type: ignore[arg-type]

    agent = _agent(here, [text("one"), text("two")])
    key = f"remote://far{there}"
    switch = WorkspaceSwitch(remotes={"far": far}, build=build)
    with _client(agent, switch) as c:
        try:
            # A new chat in a built mode: refused, and nothing is left behind.
            res = c.post(
                "/api/chat",
                json={"message": "go", "workspace": key, "workspace_mode": "coding"},
            )
            assert res.status_code == 409, res.text
            assert res.json()["detail"]["code"] == "workspace_not_allowed"
            assert c.get("/api/sessions").json() == []

            # On the agent's own policy (no mode, so no ``build``): fine.
            res = c.post("/api/chat", json={"message": "go", "workspace": key})
            assert res.status_code == 200, res.text
            sid = res.json()["session_id"]

            # Switched to a built mode later: the next run is refused the
            # same way, and the chat is otherwise as it was.
            patched = c.patch(f"/api/sessions/{sid}", json={"workspace_mode": "coding"})
            assert patched.status_code == 200, patched.text
            res = c.post("/api/chat", json={"message": "again", "session_id": sid})
            assert res.status_code == 409, res.text
            assert res.json()["detail"]["code"] == "workspace_not_allowed"
        finally:
            c.portal.call(far.aclose)


def test_a_sandboxed_handoff_target_is_caught_at_the_door_too(places) -> None:
    # The run rebuilds every agent it can reach, not just the one it starts
    # with; the check before the chat exists does the same.
    here, there = places

    class Sandbox:
        async def run(self, command, *, cwd, env, timeout, policy, root):
            raise AssertionError("never runs here")

    specialist = Agent(
        name="specialist",
        model=ScriptedProvider([text("x")]),
        workspace=Workspace.local(str(here), executor=Sandbox()),
    )
    entry = Agent(
        name="bot",
        model=ScriptedProvider([text("x")]),
        workspace=Workspace.local(str(here)),
        handoffs=[specialist],
    )
    with _client(entry, WorkspaceSwitch(remotes={"far": _far()})) as c:
        res = c.post(
            "/api/chat", json={"message": "go", "workspace": f"remote://far{there}"}
        )
        assert res.status_code == 409, res.text
        assert "executor" in res.json()["detail"]["hint"]
        assert c.get("/api/sessions").json() == []
