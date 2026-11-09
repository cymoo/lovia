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
from lovia.web.store import ChatMeta, ChatStore  # noqa: E402
from lovia.web.workspaces import ChatBinding  # noqa: E402
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
    built = WorkspaceSwitch().workspace(base, tmp_path, "readonly")
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


def test_a_live_run_passes_on_its_snapshot_none_included(tmp_path: Path) -> None:
    from lovia.web.api.deps import RouterDeps

    deps = RouterDeps(agents={}, store=ChatStore.in_memory())
    deps.workspace_switch = WorkspaceSwitch()

    class _Ctrl:
        binding = ChatBinding("/run", "readonly")

    ctrl = _Ctrl()
    stored = ChatMeta(
        id="s",
        title=None,
        agent=None,
        created_at=0,
        updated_at=0,
        workspace_root="/stored",
        workspace_mode="coding",
    )
    deps.supervisor._controllers["s"] = ctrl  # type: ignore[assignment]
    assert deps.inherited(deps.run_snapshot("s"), stored) == ctrl.binding
    # A run on the agent's own policy passes that on — not the mode the
    # chat was switched to since it began.
    ctrl.binding = ChatBinding("/run", None)
    assert deps.inherited(deps.run_snapshot("s"), stored).mode is None
    # No live run: the chat's stored binding.
    assert deps.inherited(deps.run_snapshot("other"), stored) == ChatBinding(
        "/stored", "coding"
    )
    # An inherited None stays None (the agent's own policy), whatever
    # the switch's default for page-made chats.
    deps.workspace_switch = WorkspaceSwitch(default_mode="coding")
    agent = _agent(tmp_path, [])
    assert deps.new_chat_binding(agent, None).mode is None
    assert deps.new_chat_binding(agent, "readonly").mode == "readonly"


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


# ------------------------------------------------ the permission boundary -


def test_a_handoff_cannot_widen_a_read_only_chat(tmp_path: Path) -> None:
    writer = _agent(tmp_path, [_WRITE, text("wrote")]).clone(name="writer")
    front = Agent(
        name="front",
        model=ScriptedProvider([call("transfer_to_writer", {}), text("unused")]),
        handoffs=[writer],
        workspace=Workspace.local(str(tmp_path), mode="readonly"),
    )
    with _client(front, WorkspaceSwitch()) as c:
        res = c.post("/api/chat", json={"message": "go", "workspace_mode": "readonly"})
        assert res.status_code == 200, res.text
    # The writer's own workspace allows writes; the chat's mode does not.
    assert not (tmp_path / "out.txt").exists()


def test_rebinding_handles_a_handoff_cycle(tmp_path: Path) -> None:
    from lovia.handoff import Handoff
    from lovia.web.api.deps import RouterDeps

    a = _agent(tmp_path, []).clone(name="a")
    b = _agent(tmp_path, []).clone(name="b", handoffs=[Handoff(target=a)])
    a.handoffs.append(b)  # a ⇄ b
    deps = RouterDeps(agents={"a": a}, store=ChatStore.in_memory())
    deps.workspace_switch = WorkspaceSwitch()
    bound = deps.effective_agent(a, ChatBinding(str(tmp_path), "readonly"))
    (b2,) = bound.handoffs
    assert b2.workspace.policy.write == "deny"
    assert b2.handoffs[0].target is bound  # the cycle closes on the copies
    assert a.workspace.policy.write == "allow" and a.handoffs[0] is b  # untouched


async def test_a_self_cloned_task_gets_its_own_session(tmp_path: Path) -> None:
    from lovia import Subagents

    parent = Agent(
        name="bot",
        model=ScriptedProvider(
            [call("spawn_subagent", {"prompt": "dig"}), text("ok"), text("dug")]
        ),
        plugins=[Subagents()],  # self-clone: the child is this agent, bound
        workspace=Workspace.local(str(tmp_path)),
    )
    app = create_app(
        parent,
        store=ChatStore.in_memory(),
        titles=False,
        workspaces=WorkspaceSwitch(),
    )
    deps = app.state.deps
    await deps.store.upsert("p", agent="bot", workspace_mode="coding")
    await deps.supervisor.start(
        session_id="p",
        agent=parent,
        input="go",
        is_new=False,
        title_message=None,
        autostart=True,
    )

    async def registered() -> bool:
        kids = await deps.store.list_children("p")
        return len(kids) == 1 and deps.workspaces.get(kids[0].id) is not None

    await _until(registered)
    (task,) = await deps.store.list_children("p")
    mine, theirs = deps.workspaces.get("p"), deps.workspaces.get(task.id)
    # Separate sessions: switching the parent's mode (set_policy on its
    # session) can't reach a task already running.
    assert mine is not None and theirs is not None and mine is not theirs
    mine.set_policy(Workspace.local(str(tmp_path), mode="readonly").policy)
    assert theirs.policy.write == "allow"


async def test_a_handoff_targets_subagents_run_in_the_chat_mode(
    tmp_path: Path,
) -> None:
    from lovia import Subagents

    writer = _agent(tmp_path, [_WRITE, text("wrote")]).clone(name="writer")
    plugin = Subagents(writer)
    lead = Agent(
        name="lead",
        model=ScriptedProvider(
            [call("spawn_subagent", {"prompt": "write"}), text("ok")]
        ),
        plugins=[plugin],
        workspace=Workspace.local(str(tmp_path), mode="readonly"),
    )
    front = Agent(
        name="front",
        model=ScriptedProvider([call("transfer_to_lead", {}), text("unused")]),
        handoffs=[lead],
        workspace=Workspace.local(str(tmp_path), mode="readonly"),
    )
    app = create_app(
        front, store=ChatStore.in_memory(), titles=False, workspaces=WorkspaceSwitch()
    )
    # Reached only through a handoff, and still adapted to this app.
    assert plugin.run_child is not None
    deps = app.state.deps
    await deps.store.upsert("p", agent="front", workspace_mode="readonly")
    await deps.supervisor.start(
        session_id="p",
        agent=front,
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


async def test_a_start_that_loses_a_race_never_touches_the_winners_policy(
    tmp_path: Path,
) -> None:
    import asyncio

    from lovia.web.errors import WebError

    store = ChatStore.in_memory()
    agent = _agent(tmp_path, [text("x")])
    app = create_app(agent, store=store, titles=False, workspaces=WorkspaceSwitch())
    deps = app.state.deps
    await store.upsert("s", agent="bot", workspace_mode="coding")

    # Force the interleaving that matters: the first start binds; the mode
    # is switched; a second start then binds too, and only after that does
    # the first register its run (so it wins and the second is refused).
    real_bind = deps.workspaces.bind
    second_bound = asyncio.Event()
    calls = 0
    second: asyncio.Task[object] | None = None

    async def start() -> object:
        return await deps.supervisor.start(
            session_id="s", agent=agent, input="go", is_new=False, title_message=None
        )

    async def bind(session_id: str, agent_: Agent) -> Agent:
        nonlocal calls, second
        calls += 1
        mine = calls
        bound = await real_bind(session_id, agent_)
        if mine == 1:
            await store.set_workspace_mode("s", "readonly")
            second = asyncio.create_task(start())
            try:
                await asyncio.wait_for(second_bound.wait(), 0.2)
            except asyncio.TimeoutError:
                pass  # serialized: the second start can't bind meanwhile
        else:
            second_bound.set()
            for _ in range(100):  # let the first register before refusing
                if deps.supervisor.get("s") is not None:
                    break
                await asyncio.sleep(0.005)
        return bound

    deps.workspaces.bind = bind  # type: ignore[method-assign]
    first = await start()
    assert second is not None
    results = [first, *(await asyncio.gather(second, return_exceptions=True))]
    refused = [r for r in results if isinstance(r, WebError)]
    assert len(refused) == 1 and refused[0].code == "run_active"
    # The winner runs as it was bound; the loser switched nothing under it.
    session = deps.workspaces.get("s")
    assert session is not None and session.policy.write == "allow"


def test_every_report_of_a_mode_carries_its_revision(tmp_path: Path) -> None:
    with _client(_agent(tmp_path, [text("x")]), WorkspaceSwitch()) as c:
        sid = c.post(
            "/api/chat", json={"message": "a", "workspace_mode": "coding"}
        ).json()["session_id"]
        assert c.get(f"/api/sessions/{sid}").json()["workspace_mode_rev"] == 0
        first = c.patch(f"/api/sessions/{sid}", json={"workspace_mode": "readonly"})
        second = c.patch(f"/api/sessions/{sid}", json={"workspace_mode": "coding"})
        # Each switch bumps it: the higher revision is the newer report.
        assert first.json()["workspace_mode_rev"] == 1
        assert second.json()["workspace_mode_rev"] == 2
        assert c.get(f"/api/sessions/{sid}").json()["workspace_mode_rev"] == 2
        (row,) = c.get("/api/sessions").json()
        assert (row["workspace_mode"], row["workspace_mode_rev"]) == ("coding", 2)
        # A rename doesn't count as a switch.
        c.patch(f"/api/sessions/{sid}", json={"title": "t"})
        assert c.get(f"/api/sessions/{sid}").json()["workspace_mode_rev"] == 2
