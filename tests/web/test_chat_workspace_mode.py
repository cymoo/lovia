"""Per-chat permission modes: ``create_app(workspaces=WorkspaceSwitch(...))``.

A chat stores its mode; each of its runs gets the agent's workspace rebuilt
for that mode (read when the run starts), a mode the server doesn't offer
runs capped down, and the chats a run creates — subagent tasks, spawned
chats, schedule fires — inherit the mode that run used.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.exceptions import UserError  # noqa: E402
from lovia.web import WorkspaceSwitch, create_app  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402
from lovia.workspace import Workspace, WorkspaceLimits  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402

_WRITE = call("write_file", {"path": "out.txt", "content": "hi"})


def _agent(root: Path, script: list, **ws: object) -> Agent:
    return Agent(
        name="bot",
        model=ScriptedProvider(script),
        workspace=Workspace.local(str(root), **ws),  # type: ignore[arg-type]
    )


def _client(agent: Agent, switch: WorkspaceSwitch | None) -> TestClient:
    app = create_app(
        agent, store=ChatStore.in_memory(), titles=False, workspaces=switch
    )
    return TestClient(app)


# ------------------------------------------------------------ the switch -


def test_switch_orders_modes_and_requires_the_readonly_floor() -> None:
    switch = WorkspaceSwitch(modes=("trusted", "readonly"))
    assert switch.modes == ("readonly", "trusted")
    with pytest.raises(UserError, match="readonly"):
        WorkspaceSwitch(modes=("coding",))
    with pytest.raises(UserError, match="unknown"):
        WorkspaceSwitch(modes=("readonly", "root"))  # type: ignore[arg-type]
    with pytest.raises(UserError, match="default_mode"):
        WorkspaceSwitch(default_mode="trusted")


@pytest.mark.parametrize(
    ("modes", "stored", "runs_as"),
    [
        (("readonly", "coding"), "trusted", "coding"),
        (("readonly",), "coding", "readonly"),
        (("readonly", "trusted"), "coding", "readonly"),  # never widened
        (("readonly", "coding"), "coding", "coding"),
    ],
)
def test_a_mode_the_server_does_not_offer_only_narrows(
    modes: tuple, stored: str, runs_as: str
) -> None:
    assert WorkspaceSwitch(modes=modes).effective(stored) == runs_as


def test_default_build_keeps_the_agent_workspace_settings(tmp_path: Path) -> None:
    limits = WorkspaceLimits(max_file_read_chars=7)
    base = Workspace.local(
        str(tmp_path), env={"A": "1"}, shell_timeout=9.0, limits=limits
    )
    built = WorkspaceSwitch().workspace(base, "readonly")
    assert built.policy.write == "deny"
    assert (built.root, built.env, built.shell_timeout, built.limits) == (
        str(tmp_path),
        {"A": "1"},
        9.0,
        limits,
    )


def test_create_app_refuses_a_switch_without_a_local_workspace() -> None:
    with pytest.raises(ValueError, match="local workspace"):
        create_app(
            Agent(name="x", model=ScriptedProvider([])),
            store=ChatStore.in_memory(),
            workspaces=WorkspaceSwitch(),
        )


# --------------------------------------------------------------- the API -


def test_info_reports_the_offered_modes(tmp_path: Path) -> None:
    switch = WorkspaceSwitch(default_mode="coding")
    with _client(_agent(tmp_path, []), switch) as c:
        assert c.get("/api/info").json()["workspace_modes"] == {
            "modes": ["readonly", "coding"],
            "default": "coding",
        }
    with _client(_agent(tmp_path, []), None) as c:
        assert c.get("/api/info").json()["workspace_modes"] is None


def test_a_new_chat_runs_in_the_mode_it_picked(tmp_path: Path) -> None:
    agent = _agent(tmp_path, [_WRITE, text("done")])  # coding by default
    with _client(agent, WorkspaceSwitch()) as c:
        res = c.post("/api/chat", json={"message": "go", "workspace_mode": "readonly"})
        assert res.status_code == 200, res.text
        sid = res.json()["session_id"]
        detail = c.get(f"/api/sessions/{sid}").json()
        assert detail["workspace_mode"] == "readonly"
        listed = c.get("/api/sessions").json()
        assert listed[0]["workspace_mode"] == "readonly"
    # The read-only run was refused the write the agent's own mode allows.
    assert not (tmp_path / "out.txt").exists()


def test_a_chat_without_a_mode_runs_on_the_agent_workspace(tmp_path: Path) -> None:
    agent = _agent(tmp_path, [_WRITE, text("done")])
    with _client(agent, WorkspaceSwitch()) as c:
        sid = c.post("/api/chat", json={"message": "go"}).json()["session_id"]
        assert c.get(f"/api/sessions/{sid}").json()["workspace_mode"] is None
    assert (tmp_path / "out.txt").read_text() == "hi"


def test_switching_a_chat_applies_from_its_next_run(tmp_path: Path) -> None:
    agent = _agent(tmp_path, [text("first"), _WRITE, text("second")])
    with _client(agent, WorkspaceSwitch()) as c:
        sid = c.post(
            "/api/chat", json={"message": "a", "workspace_mode": "coding"}
        ).json()["session_id"]
        res = c.patch(f"/api/sessions/{sid}", json={"workspace_mode": "readonly"})
        assert res.status_code == 200, res.text
        assert res.json()["workspace_mode"] == "readonly"
        c.post("/api/chat", json={"message": "b", "session_id": sid})
    assert not (tmp_path / "out.txt").exists()


def test_a_mode_outside_the_offer_is_refused(tmp_path: Path) -> None:
    with _client(_agent(tmp_path, [text("x")]), WorkspaceSwitch()) as c:
        res = c.post("/api/chat", json={"message": "a", "workspace_mode": "trusted"})
        assert res.status_code == 400
        sid = c.post("/api/chat", json={"message": "a"}).json()["session_id"]
        res = c.patch(f"/api/sessions/{sid}", json={"workspace_mode": "trusted"})
        assert res.status_code == 400


def test_without_a_switch_the_mode_cannot_be_set(tmp_path: Path) -> None:
    with _client(_agent(tmp_path, [text("x")]), None) as c:
        res = c.post("/api/chat", json={"message": "a", "workspace_mode": "readonly"})
        assert res.status_code == 400
        sid = c.post("/api/chat", json={"message": "a"}).json()["session_id"]
        res = c.patch(f"/api/sessions/{sid}", json={"workspace_mode": "readonly"})
        assert res.status_code == 404


def test_a_narrowed_server_caps_a_stored_wider_mode(tmp_path: Path) -> None:
    store = ChatStore.in_memory()
    agent = _agent(tmp_path, [_WRITE, text("done")], mode="trusted")
    switch = WorkspaceSwitch(modes=("readonly",))
    app = create_app(agent, store=store, titles=False, workspaces=switch)
    with TestClient(app) as c:
        # A chat stored as coding (made before the server was narrowed).
        c.portal.call(lambda: store.upsert("s", agent="bot", workspace_mode="coding"))
        res = c.post("/api/chat", json={"message": "go", "session_id": "s"})
        assert res.status_code == 200, res.text
    assert not (tmp_path / "out.txt").exists()


def test_a_new_chat_records_its_resolved_root(tmp_path: Path) -> None:
    with _client(_agent(tmp_path, [text("x")]), WorkspaceSwitch()) as c:
        sid = c.post("/api/chat", json={"message": "a"}).json()["session_id"]
        meta = c.portal.call(lambda: c.app.state.deps.store.get(sid))
    assert meta.workspace_root == tmp_path.resolve().as_posix()


# ------------------------------------------------- what a run passes on -


async def test_fork_keeps_the_source_binding(tmp_path: Path) -> None:
    store = ChatStore.in_memory()
    await store.upsert(
        "src", agent="bot", workspace_root="/r", workspace_mode="readonly"
    )
    from lovia.transcript import InputEntry

    await store.session.append("src", [InputEntry(role="user", content="hi")])
    fork = await store.fork("src")
    assert fork is not None
    meta = await store.get(fork)
    assert meta is not None
    assert (meta.workspace_root, meta.workspace_mode) == ("/r", "readonly")


def test_run_mode_is_the_live_run_snapshot(tmp_path: Path) -> None:
    from lovia.web.api.deps import RouterDeps

    deps = RouterDeps(agents={}, store=ChatStore.in_memory())
    deps.workspace_switch = WorkspaceSwitch()

    class _Ctrl:
        workspace_mode = "readonly"

    deps.supervisor._controllers["s"] = _Ctrl()  # type: ignore[assignment]
    assert deps.run_mode("s") == "readonly"
    assert deps.run_mode("other") is None
    # An inherited None stays None (the agent's own workspace), whatever
    # the switch's default for page-made chats.
    deps.workspace_switch = WorkspaceSwitch(default_mode="coding")
    agent = _agent(tmp_path, [])
    assert deps.new_chat_binding(agent, None)[1] is None
    assert deps.new_chat_binding(agent, "readonly")[1] == "readonly"


async def _until(predicate, timeout: float = 5.0) -> None:
    import asyncio

    async def loop() -> None:
        while not await predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(loop(), timeout)


async def test_a_subagent_task_runs_in_its_spawning_run_mode(tmp_path: Path) -> None:
    from lovia import Subagents

    child = _agent(tmp_path, [_WRITE, text("dug")])
    child = child.clone(name="digger")
    parent = Agent(
        name="bot",
        model=ScriptedProvider([call("spawn_subagent", {"prompt": "dig"}), text("ok")]),
        plugins=[Subagents(child)],
        workspace=Workspace.local(str(tmp_path)),
    )
    app = create_app(
        parent,
        store=ChatStore.in_memory(),
        titles=False,
        workspaces=WorkspaceSwitch(),
    )
    deps = app.state.deps
    await deps.store.upsert("p", agent="bot", workspace_mode="readonly")
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
    assert task.workspace_mode == "readonly"
    assert not (tmp_path / "out.txt").exists()


async def test_a_schedule_fire_takes_the_schedule_mode(tmp_path: Path) -> None:
    import time

    from lovia.web.scheduler import Scheduler
    from lovia.web.store import ScheduleRow

    store = ChatStore.in_memory()
    app = create_app(
        _agent(tmp_path, [_WRITE, text("done")]),
        store=store,
        titles=False,
        workspaces=WorkspaceSwitch(),
    )
    deps = app.state.deps
    now = time.time()
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
            workspace_mode="readonly",
        )
    )
    row = await store.get_schedule("sch")
    assert row is not None and row.workspace_mode == "readonly"
    target = await Scheduler(deps).fire_now(row)
    assert target is not None

    async def finished() -> bool:
        return deps.supervisor.get(target) is None

    await _until(finished)
    meta = await store.get(target)
    assert meta is not None and meta.workspace_mode == "readonly"
    assert not (tmp_path / "out.txt").exists()
