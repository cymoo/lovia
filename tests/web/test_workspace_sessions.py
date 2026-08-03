"""Chat-scoped workspace sessions and the background-process endpoints.

The point under test: a background process started during one run survives
the run's end and dies with the *chat* (deletion / shutdown) — plus the
panel's list/kill endpoints over it. Uses real processes (``sleep``) whose
liveness is asserted via ``os.kill(pid, 0)``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402
from lovia.web.workspaces import WorkspaceSessions  # noqa: E402
from lovia.workspace import Workspace  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402

# Writes the (exec-preserved) shell pid to a file, then parks. The pid is how
# tests observe process liveness from outside the app.
_SPAWN_CMD = "echo $$ > pid.txt; exec sleep 30"


def _pid(root: Path) -> int:
    deadline = time.time() + 5
    pid_file = root / "pid.txt"
    while time.time() < deadline:
        raw = pid_file.read_text().strip() if pid_file.exists() else ""
        if raw:
            return int(raw)
        time.sleep(0.02)
    raise AssertionError("background command never wrote pid.txt")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return False


def _ws_agent(root: Path, script) -> Agent:
    return Agent(
        name="bot",
        model=ScriptedProvider(script),
        workspace=Workspace.local(str(root), mode="trusted"),
    )


def _app(agent, **kw):
    kw.setdefault("generate_titles", False)
    kw.setdefault("store", ChatStore.in_memory())
    return create_app(agent, **kw)


_BG_CALL = call("shell", {"command": _SPAWN_CMD, "background": True})


# ------------------------------------------------------------- the regression -


def test_background_process_survives_run_end_and_dies_with_chat(tmp_path) -> None:
    agent = _ws_agent(
        tmp_path,
        [_BG_CALL, text("started"), text("second run, same chat")],
    )
    # `with` runs everything on the portal's one event loop — the same
    # single-loop world uvicorn provides (per-request loops would tear the
    # session's asyncio state apart between calls).
    with TestClient(_app(agent)) as c:
        sid = c.post("/api/chat", json={"message": "start a server"}).json()[
            "session_id"
        ]
        pid = _pid(tmp_path)

        # The run is over; the process must still be alive (this is the fix —
        # per-run sessions used to kill it right here).
        assert _alive(pid)
        procs = c.get(f"/api/sessions/{sid}/processes").json()
        assert [p["status"] for p in procs] == ["running"]
        assert procs[0]["command"] == _SPAWN_CMD

        # A second run in the same chat reuses the same session: the process
        # stays alive, and its id stays visible.
        c.post("/api/chat", json={"message": "still there?", "session_id": sid})
        assert _alive(pid)
        assert c.get(f"/api/sessions/{sid}/processes").json()[0]["status"] == "running"

        # Deleting the chat is what kills it.
        assert c.delete(f"/api/sessions/{sid}").json() == {"ok": True}
        assert _wait_dead(pid)
        assert c.get(f"/api/sessions/{sid}/processes").json() == []


def test_kill_endpoint_kills_and_reports(tmp_path) -> None:
    agent = _ws_agent(tmp_path, [_BG_CALL, text("started")])
    with TestClient(_app(agent)) as c:
        sid = c.post("/api/chat", json={"message": "go"}).json()["session_id"]
        pid = _pid(tmp_path)
        procs = c.get(f"/api/sessions/{sid}/processes").json()
        process_id = procs[0]["process_id"]

        refreshed = c.post(f"/api/sessions/{sid}/processes/{process_id}/kill").json()
        assert [p["status"] for p in refreshed] == ["killed"]
        assert _wait_dead(pid)

        # Unknown ids 404; a dead-but-known process stays reportable via GET.
        assert c.post(f"/api/sessions/{sid}/processes/nope/kill").status_code == 404
        assert c.get(f"/api/sessions/{sid}/processes").json()[0]["status"] == "killed"


def test_processes_empty_without_live_session(tmp_path) -> None:
    agent = _ws_agent(tmp_path, [text("hi")])
    with TestClient(_app(agent)) as c:
        # Never-ran chat id: no session, no processes — and kill has nothing.
        assert c.get("/api/sessions/nosuch/processes").json() == []
        assert c.post("/api/sessions/nosuch/processes/x/kill").status_code == 404


def test_shutdown_reaps_background_processes(tmp_path) -> None:
    agent = _ws_agent(tmp_path, [_BG_CALL, text("started")])
    app = _app(agent)
    with TestClient(app) as c:  # context manager runs the lifespan
        c.post("/api/chat", json={"message": "go"})
        pid = _pid(tmp_path)
        assert _alive(pid)
    # Lifespan shutdown closed every chat's workspace session.
    assert _wait_dead(pid)


def test_delete_all_sessions_reaps(tmp_path) -> None:
    agent = _ws_agent(tmp_path, [_BG_CALL, text("started")])
    with TestClient(_app(agent)) as c:
        c.post("/api/chat", json={"message": "go"})
        pid = _pid(tmp_path)
        assert c.delete("/api/sessions").json() == {"ok": True}
        assert _wait_dead(pid)


# ------------------------------------------------------------- registry unit -


async def test_registry_binds_one_session_per_chat(tmp_path) -> None:
    ws = Workspace.local(str(tmp_path))
    agent = Agent(name="t", model=None, workspace=ws)
    reg = WorkspaceSessions()

    b1 = await reg.bind("chat1", agent)
    b2 = await reg.bind("chat1", agent)
    assert (await b1.workspace.open()) is (await b2.workspace.open())
    assert b1.workspace.close_after_run is False
    assert agent.workspace is ws  # original config object untouched

    b3 = await reg.bind("chat2", agent)
    assert (await b3.workspace.open()) is not (await b1.workspace.open())

    await reg.aclose()
    assert reg.get("chat1") is None and reg.get("chat2") is None


async def test_registry_passthrough_without_local_workspace(tmp_path) -> None:
    reg = WorkspaceSessions()
    plain = Agent(name="p", model=None)
    assert (await reg.bind("chat", plain)) is plain
    assert reg.get("chat") is None
    await reg.close("chat")  # idempotent no-op


async def test_registry_reopens_on_workspace_config_change(tmp_path) -> None:
    reg = WorkspaceSessions()
    first = Agent(name="a", model=None, workspace=Workspace.local(str(tmp_path)))
    bound1 = await reg.bind("chat", first)
    s1 = await bound1.workspace.open()
    start = await s1.spawn("sleep 30")

    other_root = tmp_path / "other"
    other_root.mkdir()
    second = Agent(name="b", model=None, workspace=Workspace.local(str(other_root)))
    bound2 = await reg.bind("chat", second)
    s2 = await bound2.workspace.open()
    assert s2 is not s1
    # The old session was closed — its background process died with it.
    assert _wait_dead(start.pid)
    await reg.aclose()
