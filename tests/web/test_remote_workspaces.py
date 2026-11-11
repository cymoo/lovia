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


def test_lovia_web_keeps_this_machines_project_out_of_a_remote_chat(
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
    (launch / "proj-skills" / "deploy").mkdir(parents=True)
    (launch / "proj-skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: ship it\n---\nsteps", encoding="utf-8"
    )
    (launch / "AGENTS.md").write_text("LAUNCH DIRECTORY RULES", encoding="utf-8")
    config = WebConfig.model_validate({"skills": {"dirs": ["proj-skills"]}})
    served = project_agent(Agent(name="bot", model=None), launch, config)
    assert "LAUNCH DIRECTORY RULES" in served.instructions
    assert any(isinstance(p, Skills) for p in served.plugins)

    remote = remote_agent(served, config)
    assert remote.instructions == GENERIC_INSTRUCTIONS
    assert not any(isinstance(p, Skills) for p in remote.plugins)
    # The server-wide prompt (Settings → Agent) still applies everywhere.
    custom = WebConfig.model_validate({"agent": {"instructions": "be terse"}})
    assert remote_agent(served, custom).instructions == "be terse"


def test_attachments_to_a_remote_chat_are_refused_not_dropped(places) -> None:
    here, there = places
    far = _far()
    attachment = {"path": "uploads/a.png", "mime": "image/png", "kind": "image"}
    with _client(_agent(here, [text("x")]), WorkspaceSwitch(remotes={"far": far})) as c:
        for message in ("look at this", ""):
            res = c.post(
                "/api/chat",
                json={
                    "message": message,
                    "workspace": f"remote://far{there}",
                    "attachments": [attachment],
                },
            )
            assert res.status_code == 404, res.text
            assert res.json()["detail"]["code"] == "feature_unavailable"
        # Nothing was created for the refused turns.
        assert c.get("/api/sessions").json() == []


def test_every_way_in_refuses_attachments_to_a_remote_chat(places) -> None:
    # Send, stream and inject all fold attachments in with the same function;
    # the refusal lives there, so none of them can drop one quietly. (Inject
    # hands it the live run's agent: its workspace bound to the chat's session.)
    from lovia.web.attachments import user_content
    from lovia.web.errors import WebError
    from lovia.web.schemas import Attachment
    from lovia.workspace.workspace import WorkspaceSessionBinding

    here, there = places
    remote = Workspace.remote(_far(), str(there))
    attachment = Attachment(path="uploads/a.png", mime="image/png", kind="image")
    for workspace in (remote, WorkspaceSessionBinding(remote, None)):  # type: ignore[arg-type]
        agent = Agent(name="bot", model=None, workspace=workspace)
        for message in ("look at this", ""):
            with pytest.raises(WebError) as refused:
                user_content(message, [attachment], agent)
            assert refused.value.code == "feature_unavailable"
        # No attachments: nothing to refuse.
        assert user_content("just text", None, agent) == "just text"
    # A local chat is untouched (a missing file is dropped, as before).
    local = Agent(name="bot", model=None, workspace=Workspace.local(str(here)))
    assert user_content("hi", [attachment], local) == "hi"


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
    base = agent.workspace
    with pytest.raises(UserError, match="executor"):
        switch.remote_workspace(base, WorkspaceRef.parse(key), None)


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

    def build(root: Path, mode: str):
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
