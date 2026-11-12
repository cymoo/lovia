"""Tests for the /api/memory endpoints (the sidebar Memory editor).

The routes are a thin shell over ``Memory.notes_body`` / ``Memory.replace_notes``
(policy is tested with the plugin); here we pin discovery (feature flag +
per-agent ``memory`` flag), the GET/PUT round-trip with its meter fields, and
the 404 shape for agents without the plugin.
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
from lovia.plugins.memory import Memory  # noqa: E402
from lovia.plugins.memory import plugin as plugin_mod  # noqa: E402
from lovia.web import WorkspaceSwitch, chat_project, create_app  # noqa: E402
from lovia.web.store import ChatStore, ScheduleRow  # noqa: E402
from lovia.workspace import Workspace  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory(tmp_path / "mem", index=None, auto_curate=False)


@pytest.fixture()
def client(mem: Memory) -> TestClient:
    bot = Agent(name="bot", model=ScriptedProvider([text("hi")]), plugins=[mem])
    plain = Agent(name="plain", model=ScriptedProvider([text("hi")]))
    app = create_app(
        {"bot": bot, "plain": plain},
        store=ChatStore.in_memory(),
        titles=False,
    )
    return TestClient(app)


# ------------------------------------------------------------- discovery -


def test_feature_flag_and_agent_info(client: TestClient) -> None:
    assert client.get("/api/info").json()["features"]["memory"] is True
    agents = {a["name"]: a["memory"] for a in client.get("/api/agents").json()}
    assert agents == {"bot": True, "plain": False}


def test_feature_flag_false_without_any_memory() -> None:
    app = create_app(
        {"solo": Agent(name="solo", model=ScriptedProvider([text("hi")]))},
        store=ChatStore.in_memory(),
        titles=False,
    )
    c = TestClient(app)
    assert c.get("/api/info").json()["features"]["memory"] is False


def test_agent_without_memory_404s(client: TestClient) -> None:
    assert client.get("/api/memory", params={"agent": "plain"}).status_code == 404
    r = client.put("/api/memory", params={"agent": "plain"}, json={"content": "- x"})
    assert r.status_code == 404
    # None named → the default (first) agent, which keeps memory here.
    assert client.get("/api/memory").status_code == 200
    assert client.get("/api/memory", params={"agent": "nope"}).status_code == 404


# ------------------------------------------------------------ round-trip -


def test_get_empty_notes(client: TestClient, mem: Memory) -> None:
    data = client.get("/api/memory", params={"agent": "bot"}).json()
    assert data == {
        "content": "",
        "used": 0,
        "budget": mem.notes_budget,
        "dreamed_at": None,
        "layer": "user",
        "scope_key": None,
    }


async def test_get_reflects_plugin_writes(
    client: TestClient, mem: Memory, monkeypatch
) -> None:
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    await mem.remember("likes jazz")
    line = "- [2026-01] likes jazz"
    data = client.get("/api/memory", params={"agent": "bot"}).json()
    assert data["content"] == line
    assert data["used"] == len(line)


def test_put_normalizes_and_round_trips(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    body = "- uses  vim   daily\nstray prose line\n- USES VIM DAILY\n- speaks French\n"
    r = client.put("/api/memory", params={"agent": "bot"}, json={"content": body})
    assert r.status_code == 200
    data = r.json()
    # Canonical form: bullets only, whitespace collapsed, keyed dedup, and a
    # [YYYY-MM] stamp on lines the editor added without one.
    assert data["content"] == "- [2026-01] uses vim daily\n- [2026-01] speaks French"
    assert data["used"] == len(data["content"])

    again = client.get("/api/memory", params={"agent": "bot"}).json()
    assert again == data

    # The default store is the human-editable MEMORY.md under the plugin root.
    assert (tmp_path / "mem" / "MEMORY.md").read_text() == data["content"]

    # An empty body clears the notes.
    wiped = client.put(
        "/api/memory", params={"agent": "bot"}, json={"content": ""}
    ).json()
    assert wiped["content"] == "" and wiped["used"] == 0


# ----------------------------------------------------------------- dream -


async def test_dream_endpoint_tidies_and_reports(
    client: TestClient, mem: Memory, monkeypatch
) -> None:
    async def fake_dream(body, max_chars, model):
        return ["[2026-01] merged note"]

    monkeypatch.setattr(plugin_mod, "_dream", fake_dream)
    await mem.remember("fact one")
    await mem.remember("fact two")

    data = client.post("/api/memory/dream", params={"agent": "bot"}).json()
    assert (data["before"], data["after"]) == (2, 1)
    assert data["content"] == "- [2026-01] merged note"
    assert data["used"] == len(data["content"])
    assert data["dreamed_at"] is not None

    # The editor's follow-up GET sees the tidied notes and the timestamp.
    again = client.get("/api/memory", params={"agent": "bot"}).json()
    assert again["content"] == data["content"]
    assert again["dreamed_at"] == data["dreamed_at"]


def test_dream_endpoint_404s_without_memory(client: TestClient) -> None:
    assert (
        client.post("/api/memory/dream", params={"agent": "plain"}).status_code == 404
    )


# -------------------------------------------------------------- projects -


@pytest.fixture
def proj(tmp_path: Path) -> dict[str, Path]:
    """The agent's own directory, another project, and General."""
    out = {n: tmp_path / n for n in ("launch", "other", "general")}
    out["launch"].mkdir()
    out["other"].mkdir()
    return out


def _scoped(tmp_path: Path) -> Memory:
    return Memory(
        tmp_path / "mem",
        index=None,
        auto_curate=False,
        scope=chat_project,
        scope_name="project",
    )


def _projects_client(
    mem: Memory, proj: dict[str, Path], script: list, **kw: Any
) -> TestClient:
    bot = Agent(
        name="bot",
        model=ScriptedProvider(script),
        workspace=Workspace.local(str(proj["launch"])),
        plugins=[mem],
    )
    switch = WorkspaceSwitch(general=proj["general"], **kw)
    app = create_app(bot, store=ChatStore.in_memory(), titles=False, workspaces=switch)
    return TestClient(app)


def _key(path: Path) -> str:
    return path.resolve().as_posix()


def _remember(fact: str, **about: str) -> Any:
    return call("remember", {"fact": fact, **about})


def test_a_run_remembers_into_its_chat_project(
    tmp_path: Path, proj: dict[str, Path], monkeypatch
) -> None:
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    mem = _scoped(tmp_path)
    script = [
        _remember("builds with uv", about="project"),
        _remember("prefers short answers", about="user"),
        text("noted"),
    ]
    with _projects_client(mem, proj, script) as c:
        assert c.get("/api/agents").json()[0]["memory_projects"] is True
        res = c.post(
            "/api/chat", json={"message": "hi", "workspace": str(proj["other"])}
        )
        assert res.status_code == 200, res.text
        sid = res.json()["session_id"]

        key = _key(proj["other"])
        data = c.get("/api/memory", params={"layer": "project", "session": sid}).json()
        assert data["content"] == "- [2026-01] builds with uv"
        assert (data["layer"], data["scope_key"]) == ("project", key)
        # A draft names the same project by its directory.
        draft = c.get(
            "/api/memory", params={"layer": "project", "root": str(proj["other"])}
        ).json()
        assert draft == data
        user = c.get("/api/memory", params={"session": sid}).json()
        assert user["content"] == "- [2026-01] prefers short answers"
        assert user["scope_key"] is None
        # The agent's own directory is a project of its own, still empty.
        own = c.get("/api/memory", params={"layer": "project"}).json()
        assert (own["content"], own["scope_key"]) == ("", _key(proj["launch"]))

        # Edit and tidy a project's notes; the user's stay as they were.
        put = c.put(
            "/api/memory",
            params={"layer": "project", "session": sid},
            json={"content": "- [2026-01] builds with uv\n- [2026-01] CI on Actions"},
        ).json()
        assert put["scope_key"] == key and put["content"].count("\n") == 1

        async def fake_dream(body, max_chars, model):
            return ["[2026-01] uv + Actions"]

        monkeypatch.setattr(plugin_mod, "_dream", fake_dream)
        dreamed = c.post(
            "/api/memory/dream", params={"layer": "project", "session": sid}
        ).json()
        assert (dreamed["before"], dreamed["after"]) == (2, 1)
        assert dreamed["dreamed_at"] is not None
        assert c.get("/api/memory").json()["content"] == user["content"]

        # Clearing a project forgets it; "about me" has no clear.
        cleared = c.delete("/api/memory", params={"layer": "project", "session": sid})
        assert cleared.status_code == 200
        assert cleared.json()["content"] == ""
        assert c.get("/api/memory").json()["content"] == user["content"]
        assert c.delete("/api/memory", params={"session": sid}).status_code == 400


async def test_a_general_chat_keeps_only_user_memory(
    tmp_path: Path, proj: dict[str, Path], monkeypatch
) -> None:
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    mem = _scoped(tmp_path)
    # In General, remember takes no `about`: there is one layer.
    with _projects_client(mem, proj, [_remember("lives in Lisbon"), text("ok")]) as c:
        res = c.post(
            "/api/chat", json={"message": "hi", "workspace": str(proj["general"])}
        )
        sid = res.json()["session_id"]
        r = c.get("/api/memory", params={"layer": "project", "session": sid})
        assert r.status_code == 400
        draft = {"layer": "project", "root": str(proj["general"])}
        assert c.get("/api/memory", params=draft).status_code == 400
        assert c.get("/api/memory").json()["content"] == "- [2026-01] lives in Lisbon"
    assert not (tmp_path / "mem" / "scopes").exists()


def test_a_refused_directory_is_a_409(tmp_path: Path, proj: dict[str, Path]) -> None:
    mem = _scoped(tmp_path)
    with _projects_client(mem, proj, [text("hi")], roots=None) as c:
        r = c.get(
            "/api/memory", params={"layer": "project", "root": str(proj["other"])}
        )
        assert r.status_code == 409


def test_without_a_workspace_there_is_no_project(tmp_path: Path) -> None:
    mem = _scoped(tmp_path)
    bot = Agent(name="bot", model=ScriptedProvider([text("hi")]), plugins=[mem])
    with TestClient(create_app(bot, store=ChatStore.in_memory(), titles=False)) as c:
        assert c.get("/api/memory", params={"layer": "project"}).status_code == 400
        assert c.get("/api/memory").status_code == 200


def test_a_memory_scoped_otherwise_has_no_project_tab(
    client: TestClient, tmp_path: Path
) -> None:
    # The fixture's Memory has no scope; a custom one keys by something the
    # editor can't name. Either way: the user's layer only.
    assert client.get("/api/agents").json()[0]["memory_projects"] is False
    r = client.get("/api/memory", params={"agent": "bot", "layer": "project"})
    assert r.status_code == 404
    custom = Memory(tmp_path / "m2", index=None, scope=lambda ctx: "x")
    bot = Agent(name="bot", model=ScriptedProvider([text("hi")]), plugins=[custom])
    with TestClient(create_app(bot, store=ChatStore.in_memory(), titles=False)) as c:
        assert c.get("/api/agents").json()[0]["memory_projects"] is False


async def test_a_schedule_run_remembers_into_its_directory(
    tmp_path: Path, proj: dict[str, Path], monkeypatch
) -> None:
    from lovia.web.scheduler import Scheduler

    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    mem = _scoped(tmp_path)
    store = ChatStore.in_memory()
    bot = Agent(
        name="bot",
        model=ScriptedProvider(
            [_remember("nightly build", about="project"), text("ok")]
        ),
        workspace=Workspace.local(str(proj["launch"])),
        plugins=[mem],
    )
    app = create_app(bot, store=store, titles=False, workspaces=WorkspaceSwitch())
    deps = app.state.deps
    now = time.time()
    root = _key(proj["other"])
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
            workspace_root=root,
        )
    )
    row = await store.get_schedule("sch")
    assert row is not None
    target = await Scheduler(deps).fire_now(row)
    assert target is not None
    for _ in range(500):
        if deps.supervisor.get(target) is None:
            break
        await asyncio.sleep(0.01)
    assert await mem.notes_body(scope=root) == "- [2026-01] nightly build"
    assert await mem.notes_body() == ""


async def test_a_remote_chat_keys_its_project_by_location(tmp_path: Path) -> None:
    from lovia.web.api.deps import RouterDeps
    from lovia.web.workspaces import ChatBinding

    deps = RouterDeps(
        agents={"bot": Agent(name="bot")},
        store=ChatStore.in_memory(),
        workspace_switch=WorkspaceSwitch(general=tmp_path / "g"),
    )
    key = "ssh://box/srv/app"
    assert deps.project_of(ChatBinding(key, None)) == key
    assert deps.project_of(ChatBinding((tmp_path / "g").as_posix(), None)) is None
    assert deps.project_of(ChatBinding(None, None)) is None
    mem = _scoped(tmp_path)
    await mem.remember("deploys with systemd", scope=key)
    (meta,) = (tmp_path / "mem" / "scopes").glob("app-*/scope.json")
    assert key in meta.read_text()


# ------------------------------------------------------------- shutdown -


def test_shutdown_drains_background_curation(tmp_path: Path, monkeypatch) -> None:
    # A clean server stop must not drop the last run's curation: the digest
    # below is still sleeping when the lifespan shutdown begins, and only the
    # drain in create_app's lifespan gets it onto disk.
    async def slow_digest(entries, current, model):
        await asyncio.sleep(0.3)
        return plugin_mod._RunDigest(facts=["survives shutdown"], summary="")

    monkeypatch.setattr(plugin_mod, "_digest", slow_digest)
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    mem = Memory(tmp_path / "mem", index=None, curate_in_background=True)
    bot = Agent(name="bot", model=ScriptedProvider([text("hi")]), plugins=[mem])
    app = create_app({"bot": bot}, store=ChatStore.in_memory(), titles=False)

    with TestClient(app) as client:  # the context manager runs the lifespan
        assert client.post("/api/chat", json={"message": "hello"}).status_code == 200
    body = (tmp_path / "mem" / "MEMORY.md").read_text()
    assert body == "- [2026-01] survives shutdown"
