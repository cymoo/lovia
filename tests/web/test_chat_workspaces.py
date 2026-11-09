"""Chats in several directories: ``WorkspaceSwitch(roots=..., general=...,
agent_for=...)``, the ``/api/workspaces`` list, and binding chats from before
chats had a directory.

A chat is bound to a directory when it is created and keeps it; every run
checks it against the server's constraints first (refused, never moved), and
everything a run reads or creates — attachments, the Files panel, subagent
tasks, spawned chats, schedule fires — works in that directory.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.exceptions import UserError  # noqa: E402
from lovia.web import WorkspaceSwitch, create_app  # noqa: E402
from lovia.web.store import ChatStore, ScheduleRow  # noqa: E402
from lovia.web.workspaces import ChatBinding  # noqa: E402
from lovia.workspace import Workspace  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402

_WRITE = call("write_file", {"path": "out.txt", "content": "hi"})


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """``(launch, other)``: the agent's own directory and another one."""
    launch, other = tmp_path / "launch", tmp_path / "other"
    launch.mkdir()
    other.mkdir()
    return launch, other


def _agent(root: Path, script: list, **kw: Any) -> Agent:
    return Agent(
        name="bot",
        model=ScriptedProvider(script),
        workspace=Workspace.local(str(root)),
        **kw,
    )


def _client(
    agent: Agent, switch: WorkspaceSwitch | None, store: ChatStore | None = None
) -> TestClient:
    app = create_app(
        agent, store=store or ChatStore.in_memory(), titles=False, workspaces=switch
    )
    return TestClient(app)


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    async def loop() -> None:
        while not await predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(loop(), timeout)


# ------------------------------------------------------------ the switch -


def test_roots_resolve_and_bound_what_a_chat_may_open(tmp_path: Path) -> None:
    allowed = tmp_path / "repos"
    switch = WorkspaceSwitch(roots=[str(allowed)], general=tmp_path / "g")
    own = tmp_path / "own"
    assert switch.roots == (allowed.resolve(),)
    assert switch.refusal(allowed / "x", own) is None  # under a root
    assert switch.refusal(own, own) is None  # the agent's own
    assert switch.refusal((tmp_path / "g").resolve(), own) is None  # general
    why = switch.refusal(tmp_path / "elsewhere", own)
    assert why is not None and "only directories under" in why
    fixed = WorkspaceSwitch(roots=None)
    assert "keeps every chat in" in (fixed.refusal(tmp_path / "x", own) or "")
    assert WorkspaceSwitch().refusal(tmp_path / "anything", own) is None
    with pytest.raises(UserError, match="list"):
        WorkspaceSwitch(roots=str(allowed))  # type: ignore[arg-type]


def test_a_none_mode_keeps_the_agent_policy_moved_to_the_chat_dir(
    dirs: tuple[Path, Path],
) -> None:
    launch, other = dirs
    base = Workspace.local(str(launch), mode="readonly", denied_paths=("secret/**",))
    moved = WorkspaceSwitch().workspace(base, other, None)
    assert moved.root == str(other)
    assert moved.policy == base.policy


# -------------------------------------------------------- new chats & runs -


def test_a_new_chat_works_in_the_directory_it_picked(dirs: tuple[Path, Path]) -> None:
    launch, other = dirs
    agent = _agent(launch, [_WRITE, text("done")])
    with _client(agent, WorkspaceSwitch()) as c:
        res = c.post(
            "/api/chat",
            json={"message": "go", "workspace": str(other), "workspace_mode": "coding"},
        )
        assert res.status_code == 200, res.text
        sid = res.json()["session_id"]
        assert c.get(f"/api/sessions/{sid}").json()["workspace_root"] == (
            other.resolve().as_posix()
        )
        (listed,) = [s for s in c.get("/api/sessions").json() if s["id"] == sid]
        assert listed["workspace_root"] == other.resolve().as_posix()
        roots = [w["root"] for w in c.get("/api/workspaces").json()["workspaces"]]
        assert other.resolve().as_posix() in roots  # used → listed
    assert (other / "out.txt").read_text() == "hi"
    assert not (launch / "out.txt").exists()


def test_a_new_chat_defaults_to_the_agent_directory(dirs: tuple[Path, Path]) -> None:
    launch, _ = dirs
    with _client(_agent(launch, [text("hi")]), WorkspaceSwitch()) as c:
        sid = c.post("/api/chat", json={"message": "go"}).json()["session_id"]
        detail = c.get(f"/api/sessions/{sid}").json()
        assert detail["workspace_root"] == launch.resolve().as_posix()


@pytest.mark.parametrize(
    ("ask", "status", "code"),
    [
        ("relative/dir", 400, "invalid_request"),
        ("{other}/missing", 409, "workspace_missing"),
        ("{elsewhere}", 409, "workspace_not_allowed"),
    ],
)
def test_a_refused_directory_writes_nothing(
    tmp_path: Path, dirs: tuple[Path, Path], ask: str, status: int, code: str
) -> None:
    launch, other = dirs
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    switch = WorkspaceSwitch(roots=[str(other)])
    store = ChatStore.in_memory()
    with _client(_agent(launch, [text("never")]), switch, store) as c:
        res = c.post(
            "/api/chat",
            json={
                "message": "go",
                "session_id": "s1",
                "workspace": ask.format(other=other, elsewhere=elsewhere),
            },
        )
        assert res.status_code == status, res.text
        assert res.json()["detail"]["code"] == code
        assert c.get("/api/sessions").json() == []


def test_a_server_without_a_switch_refuses_a_directory(
    dirs: tuple[Path, Path],
) -> None:
    launch, other = dirs
    with _client(_agent(launch, []), None) as c:
        res = c.post("/api/chat", json={"message": "go", "workspace": str(other)})
        assert res.status_code == 400


def test_a_chat_in_a_directory_the_server_no_longer_allows_is_refused(
    tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    launch, other = dirs
    db = tmp_path / "chat.db"
    with _client(
        _agent(launch, [text("one")]), WorkspaceSwitch(), ChatStore.sqlite(db)
    ) as c:
        sid = c.post(
            "/api/chat", json={"message": "go", "workspace": str(other)}
        ).json()["session_id"]
    # The same chats (a restart: one store per server) under a server locked
    # to its own directory.
    agent = _agent(launch, [_WRITE, text("never")])
    with _client(agent, WorkspaceSwitch(roots=None), ChatStore.sqlite(db)) as c:
        res = c.post("/api/chat", json={"message": "again", "session_id": sid})
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "workspace_not_allowed"
        # Nor do its files show.
        res = c.get("/api/workspace/recent", params={"session": sid})
        assert res.status_code == 409
    assert not (launch / "out.txt").exists() and not (other / "out.txt").exists()


def test_the_general_directory_is_created_on_first_use(
    tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    launch, _ = dirs
    general = tmp_path / "home" / ".lovia" / "workspace"
    switch = WorkspaceSwitch(roots=None, general=general)
    with _client(_agent(launch, [_WRITE, text("ok")]), switch) as c:
        items = c.get("/api/workspaces").json()["workspaces"]
        assert [i["kind"] for i in items] == ["default", "general"]
        assert not general.exists()  # listed, not made
        res = c.post("/api/chat", json={"message": "go", "workspace": str(general)})
        assert res.status_code == 200, res.text
    assert (general / "out.txt").is_file()


def test_attachments_resolve_in_the_draft_directory(dirs: tuple[Path, Path]) -> None:
    launch, other = dirs
    provider = ScriptedProvider([text("seen")])
    agent = Agent(name="bot", model=provider, workspace=Workspace.local(str(launch)))
    with _client(agent, WorkspaceSwitch()) as c:
        up = c.post(
            "/api/workspace/upload",
            params={"root": str(other)},
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert up.status_code == 200, up.text
        att = up.json()
        assert (other / att["path"]).is_file()
        res = c.post(
            "/api/chat",
            json={
                "message": "read it",
                "workspace": str(other),
                "attachments": [
                    {"path": att["path"], "mime": att["mime"], "kind": att["kind"]}
                ],
            },
        )
        assert res.status_code == 200, res.text
    sent = str(provider.calls[0][-1].content)
    assert att["path"] in sent  # kept: it exists in the chat's directory


def test_files_panel_reads_the_chat_directory(dirs: tuple[Path, Path]) -> None:
    launch, other = dirs
    (other / "here.txt").write_text("x")
    (launch / "there.txt").write_text("y")
    with _client(_agent(launch, [text("ok")]), WorkspaceSwitch()) as c:
        sid = c.post(
            "/api/chat", json={"message": "go", "workspace": str(other)}
        ).json()["session_id"]
        names = [
            e["path"] for e in c.get(f"/api/workspace/recent?session={sid}").json()
        ]
        assert names == ["here.txt"]
        info = c.get("/api/workspace", params={"root": str(other)}).json()
        assert info["name"] == "other"
        own = [e["path"] for e in c.get("/api/workspace/recent").json()]
        assert own == ["there.txt"]


# ------------------------------------------------ what a run hands on -


def test_agent_for_gets_each_run_with_its_directory(dirs: tuple[Path, Path]) -> None:
    launch, other = dirs
    seen: list[Path] = []
    provider = ScriptedProvider([text("ok")])

    def agent_for(agent: Agent, root: Path) -> Agent:
        seen.append(root)
        # A workspace set here is replaced by the chat's.
        return agent.clone(
            instructions=f"project {root.name}",
            workspace=Workspace.local(str(launch), mode="trusted"),
        )

    agent = Agent(name="bot", model=provider, workspace=Workspace.local(str(launch)))
    store = ChatStore.in_memory()
    with _client(agent, WorkspaceSwitch(agent_for=agent_for), store) as c:
        res = c.post(
            "/api/chat",
            json={
                "message": "go",
                "workspace": str(other),
                "workspace_mode": "readonly",
            },
        )
        assert res.status_code == 200, res.text
    assert seen == [other.resolve()]
    system = provider.calls[0][0]
    assert "project other" in str(system.content)


async def test_a_subagent_task_works_in_its_parent_directory(
    dirs: tuple[Path, Path],
) -> None:
    from lovia import Subagents

    launch, other = dirs
    child = _agent(launch, [_WRITE, text("dug")]).clone(name="digger")
    parent = Agent(
        name="bot",
        model=ScriptedProvider([call("spawn_subagent", {"prompt": "dig"}), text("ok")]),
        plugins=[Subagents(child)],
        workspace=Workspace.local(str(launch)),
    )
    app = create_app(
        parent, store=ChatStore.in_memory(), titles=False, workspaces=WorkspaceSwitch()
    )
    deps = app.state.deps
    root = other.resolve().as_posix()
    await deps.store.upsert(
        "p", agent="bot", workspace_root=root, workspace_mode="coding"
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

    await _until(task_done)
    (task,) = await deps.store.list_children("p")
    assert task.workspace_root == root
    assert (other / "out.txt").is_file() and not (launch / "out.txt").exists()


async def test_a_schedule_fire_works_in_the_schedule_directory(
    dirs: tuple[Path, Path],
) -> None:
    from lovia.web.scheduler import Scheduler

    launch, other = dirs
    store = ChatStore.in_memory()
    app = create_app(
        _agent(launch, [_WRITE, text("done")]),
        store=store,
        titles=False,
        workspaces=WorkspaceSwitch(),
    )
    deps = app.state.deps
    now = time.time()
    root = other.resolve().as_posix()
    await store.add_schedule(
        ScheduleRow(
            id="sch",
            agent="bot",
            input="tick",
            session_id=None,
            trigger_kind="every",
            trigger_expr="3600",
            next_fire=now + 3600,
            active=True,
            last_session_id=None,
            created_at=now,
            updated_at=now,
            workspace_mode="coding",
            workspace_root=root,
        )
    )
    row = await store.get_schedule("sch")
    assert row is not None and row.workspace_root == root
    target = await Scheduler(deps).fire_now(row)
    assert target is not None

    async def finished() -> bool:
        return deps.supervisor.get(target) is None

    await _until(finished)
    meta = await store.get(target)
    assert meta is not None and meta.workspace_root == root
    assert (other / "out.txt").is_file()


def test_a_live_run_hands_on_its_directory(dirs: tuple[Path, Path]) -> None:
    from lovia.web.api.deps import RouterDeps

    launch, other = dirs
    deps = RouterDeps(agents={}, store=ChatStore.in_memory())
    deps.workspace_switch = WorkspaceSwitch()

    class _Ctrl:
        binding = ChatBinding(other.as_posix(), "readonly")

    deps.supervisor._controllers["s"] = _Ctrl()  # type: ignore[assignment]
    inherited = deps.inherited(deps.run_snapshot("s"), None)
    agent = _agent(launch, [])
    made = deps.new_chat_binding(agent, inherited.mode, inherited.root)
    assert made == ChatBinding(other.resolve().as_posix(), "readonly")


# --------------------------------------------------------- the list API -


def test_the_list_add_remove_and_complete(
    tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    launch, other = dirs
    (other / "alpha").mkdir()
    (other / "Beta").mkdir()
    (other / ".hidden").mkdir()
    (other / "afile").write_text("")
    with _client(_agent(launch, []), WorkspaceSwitch()) as c:
        listed = c.get("/api/workspaces").json()
        assert listed["default"] == launch.resolve().as_posix()
        assert listed["can_add"] is True and listed["roots"] is None
        added = c.post("/api/workspaces", json={"path": str(other)}).json()
        assert added["kind"] == "added" and added["name"] == "other"
        assert (
            c.post("/api/workspaces", json={"path": str(tmp_path / "nope")}).status_code
            == 404
        )
        # The agent's own is always there, never duplicated.
        again = c.post("/api/workspaces", json={"path": str(launch)}).json()
        assert again["kind"] == "default" and again["id"] is None
        roots = [w["root"] for w in c.get("/api/workspaces").json()["workspaces"]]
        assert roots.count(launch.resolve().as_posix()) == 1
        assert c.get("/api/workspaces/dirs", params={"path": f"{other}/"}).json() == [
            f"{other}/alpha",
            f"{other}/Beta",
        ]
        assert c.get("/api/workspaces/dirs", params={"path": f"{other}/b"}).json() == [
            f"{other}/Beta"
        ]
        assert c.get("/api/workspaces/dirs", params={"path": f"{other}/."}).json() == [
            f"{other}/.hidden"
        ]
        assert c.delete(f"/api/workspaces/{added['id']}").status_code == 200
        assert c.delete(f"/api/workspaces/{added['id']}").status_code == 404
        roots = [w["root"] for w in c.get("/api/workspaces").json()["workspaces"]]
        assert other.resolve().as_posix() not in roots


def test_completion_stays_within_the_allowed_trees(tmp_path: Path) -> None:
    repos = tmp_path / "srv" / "repos"
    (repos / "app").mkdir(parents=True)
    (tmp_path / "srv" / "etc").mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    switch = WorkspaceSwitch(roots=[str(repos)])
    with _client(_agent(launch, []), switch) as c:
        listed = c.get("/api/workspaces").json()
        assert listed["roots"] == [repos.resolve().as_posix()]
        srv = c.get("/api/workspaces/dirs", params={"path": f"{tmp_path}/srv/"}).json()
        assert srv == [f"{tmp_path}/srv/repos"]  # on the way to a root only
        res = c.post("/api/workspaces", json={"path": str(tmp_path / "srv" / "etc")})
        assert res.json()["detail"]["code"] == "workspace_not_allowed"
    with _client(_agent(launch, []), WorkspaceSwitch(roots=None)) as c:
        assert c.get("/api/workspaces").json()["can_add"] is False
        assert c.get("/api/workspaces/dirs", params={"path": "/"}).json() == []


def test_without_a_switch_there_is_no_list(dirs: tuple[Path, Path]) -> None:
    launch, _ = dirs
    with _client(_agent(launch, []), None) as c:
        assert c.get("/api/workspaces").status_code == 404
        assert c.get("/api/info").json()["features"]["workspace_switch"] is False


# ---------------------------------------------- chats from before roots -


async def test_associating_binds_only_unbound_chats_once(
    dirs: tuple[Path, Path],
) -> None:
    launch, other = dirs
    store = ChatStore.in_memory()
    await store.upsert("old1", agent="bot")
    await store.upsert("old2", agent="bot")
    await store.upsert("new", agent="bot", workspace_root=launch.as_posix())
    with _client(_agent(launch, []), WorkspaceSwitch(), store) as c:
        res = c.post(
            "/api/sessions/associate",
            json={"session_ids": ["old1", "new", "gone"], "root": str(other)},
        )
        assert res.status_code == 200, res.text
        assert res.json() == {
            "associated": ["old1"],
            "root": other.resolve().as_posix(),
        }
        res = c.patch("/api/sessions/old2", json={"workspace_root": str(other)})
        assert res.json()["workspace_root"] == other.resolve().as_posix()
        res = c.patch("/api/sessions/old2", json={"workspace_root": str(launch)})
        assert res.json()["detail"]["code"] == "workspace_fixed"
        refused = c.post(
            "/api/sessions/associate",
            json={"session_ids": ["new"], "root": "relative"},
        )
        assert refused.status_code == 400
    meta = await store.get("new")
    assert meta is not None and meta.workspace_root == launch.as_posix()


async def test_store_migrates_the_schedule_root_and_list(tmp_path: Path) -> None:
    store = ChatStore.sqlite(tmp_path / "chat.db")
    row = await store.touch_workspace("/a/b")
    assert (row.root, row.name) == ("/a/b", "b")
    later = await store.touch_workspace("/a/b")
    assert later.id == row.id and later.last_used_at >= row.last_used_at
    assert [w.root for w in await store.list_workspaces()] == ["/a/b"]
    assert await store.remove_workspace(row.id) is True
    assert await store.list_workspaces() == []
