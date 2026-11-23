"""The Introspection plugin: what the agent can read about the app it runs in."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from lovia import Agent  # noqa: E402
from lovia.checkpointer import RunHead  # noqa: E402
from lovia.exceptions import ToolError  # noqa: E402
from lovia.messages import Usage  # noqa: E402
from lovia.run_context import RunContext  # noqa: E402
from lovia.transcript import AssistantTextEntry, InputEntry  # noqa: E402
from lovia.web import ChatStore, Introspection, WorkspaceSwitch, create_app  # noqa: E402
from lovia.web.introspection import _when, list_workspaces, read_chat  # noqa: E402
from lovia.web.stop_check import with_stop_check  # noqa: E402
from lovia.workspace import Workspace  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_scheduler import _drain_runs  # noqa: E402
from .test_supervisor import _client  # noqa: E402


async def _run(store: ChatStore, root: Path, calls: list, tmp_path: Path) -> list[str]:
    """Run chat ``s1`` in ``root`` making ``calls``; return the tool results."""
    script = [call(name, args, call_id=f"c{i}") for i, (name, args) in enumerate(calls)]
    agent = Agent(
        name="bot",
        model=ScriptedProvider([*script, text("done")]),
        workspace=Workspace.local(str(root)),
        plugins=[Introspection()],
    )
    switch = WorkspaceSwitch(roots="any", general=tmp_path / "general")
    app = create_app(agent, store=store, titles=False, workspaces=switch)
    async with _client(app) as ac:
        res = await ac.post(
            "/api/chat",
            json={"message": "go", "session_id": "s1", "workspace_mode": "readonly"},
        )
        assert res.status_code == 200
        await _drain_runs(app.state.deps)
        entries = (await ac.get("/api/sessions/s1")).json()["entries"]
    results = [e for e in entries if e["role"] == "tool"]
    assert not any(e["is_error"] for e in results), results
    return [e["content"] for e in results]


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    launch, other = tmp_path / "launch", tmp_path / "other"
    launch.mkdir()
    other.mkdir()
    return launch, other


async def test_workspaces_and_chats(tmp_path: Path, dirs: tuple[Path, Path]) -> None:
    launch, other = dirs
    store = ChatStore.in_memory()
    other_root = other.resolve().as_posix()
    await store.rename_workspace(other_root, "Tides")
    await store.upsert("a", agent="bot", title="tide tables", workspace_root=other_root)
    await store.upsert("b", agent="bot", title="moon phases", workspace_root=other_root)
    await store.upsert("c", agent="bot", title="old tides", workspace_root=other_root)
    await store.set_archived("c", True)
    await store.upsert("t", agent="bot", title="a task", parent_id="a")

    workspaces, by_name, archived, searched = await _run(
        store,
        launch,
        [
            ("list_workspaces", {}),
            ("list_chats", {"workspace": "Tides"}),
            ("list_chats", {"archived": True}),
            ("list_chats", {"query": "tide"}),
        ],
        tmp_path,
    )
    lines = workspaces.splitlines()
    # The page's list: the agent's own directory, the general one, then the
    # added ones; counts leave tasks and archived chats out.
    assert "where new chats start" in lines[0] and "this chat" in lines[0]
    assert "for chats of no project" in lines[1]
    assert lines[2].startswith(f"- Tides — {other_root} (2 chats;")
    assert [line.split(":")[0] for line in by_name.splitlines()] == ["- b", "- a"]
    assert archived.splitlines()[0].startswith("- c: old tides (")
    assert "archived" in archived
    # A title search leaves the archived ones out unless asked.
    assert [line.split(":")[0] for line in searched.splitlines()] == ["- a"]


async def test_read_chat_shows_text_only(
    tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    launch, _ = dirs
    store = ChatStore.in_memory()
    await store.upsert("old", agent="bot", title="plans")
    await store.session.append(
        "old",
        [
            InputEntry(role="system", content="hidden"),
            InputEntry(role="user", content="first question"),
            AssistantTextEntry(content="looking"),
            AssistantTextEntry(content="answer one"),
            InputEntry(role="user", content="second question"),
            AssistantTextEntry(content="answer two"),
        ],
    )
    full, page, missing = await _run(
        store,
        launch,
        [
            ("read_chat", {"chat_id": "old"}),
            ("read_chat", {"chat_id": "old", "last": 2, "skip": 1}),
            ("read_chat", {"chat_id": "old", "skip": 10}),
        ],
        tmp_path,
    )
    assert "Messages 1–4 of 4." in full
    assert "hidden" not in full
    # Assistant text between tool calls reads as one message.
    assert "[assistant] looking\n\nanswer one" in full
    assert full.endswith("[assistant] answer two")
    assert "Messages 2–3 of 4." in page
    assert page.endswith("[user] second question")
    assert missing.endswith("No messages that far back.")


async def test_read_chat_stop_check_and_unfinished_turn(
    tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    launch, _ = dirs
    store = ChatStore.in_memory()
    assert store.checkpointer is not None
    turn = [
        InputEntry(role="user", content="deploy it"),
        AssistantTextEntry(content="Running the deploy script."),
    ]
    head = RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted")
    # A schedule's fire: the stop check it appends is for that chat's agent.
    await store.upsert("fired", agent="bot", title="log watch")
    fire = with_stop_check("check the log", "sched1", "it says ready")
    await store.session.append(
        "fired",
        [InputEntry(role="user", content=fire), AssistantTextEntry(content="not yet")],
    )
    # A first turn stopped on an approval: in its checkpoint only.
    await store.upsert("pending", agent="bot", title="deploy")
    await store.checkpointer.append("r9", turn, head)
    await store.set_active_run_id("pending", "r9")
    # Stopped mid-read: the run is in the Session, its checkpoint not yet gone.
    await store.upsert("stopped", agent="bot", title="deploy")
    await store.session.append("stopped", turn, run_id="r8")
    await store.checkpointer.append("r8", turn, head)
    await store.set_active_run_id("stopped", "r8")
    fired, pending, stopped = await _run(
        store,
        launch,
        [
            ("read_chat", {"chat_id": "fired"}),
            ("read_chat", {"chat_id": "pending"}),
            ("read_chat", {"chat_id": "stopped"}),
        ],
        tmp_path,
    )
    assert "[user] check the log\n\n[assistant] not yet" in fired
    assert "cancel_schedule" not in fired
    assert "Messages 1–2 of 2. Its latest turn hasn't finished" in pending
    assert pending.endswith("[assistant] Running the deploy script.")
    assert "Messages 1–2 of 2.\n" in stopped
    assert stopped.count("deploy it") == 1


async def test_lovia_status_names_this_chat(
    tmp_path: Path, dirs: tuple[Path, Path]
) -> None:
    launch, _ = dirs
    (status,) = await _run(
        ChatStore.in_memory(), launch, [("lovia_status", {})], tmp_path
    )
    assert status.startswith("lovia ")
    assert "This chat: s1" in status
    assert f"- workspace: launch ({launch.resolve().as_posix()})" in status
    assert "- mode: readonly" in status
    assert "- model of this reply:" in status


async def test_lovia_status_models_and_mcp() -> None:
    from lovia.web.config.schema import MCPServerConfig, WebConfig
    from lovia.web.introspection import lovia_status
    from lovia.web.mcp import MCPBook, MCPStatus, SettingsMCP
    from lovia.web.run_deps import RUN_SERVER

    store = ChatStore.in_memory()
    await store.upsert("s1", agent="bot", title="hi", model="big")
    config = WebConfig.model_validate(
        {
            "models": [
                {"id": "fast", "model": "gpt-x"},
                {"id": "big", "model": "gpt-y", "name": "Big"},
            ],
            "roles": {"chat": "fast"},
        }
    )
    up = MCPServerConfig(name="files", command="x")
    down = MCPServerConfig(name="web", command="y")
    book = MCPBook()
    book.record(up, MCPStatus(ok=True, at=200.0, tools=("a", "b")))
    book.record(down, MCPStatus(ok=False, at=50.0, error="secret detail"))
    servers = [up, down, MCPServerConfig(name="off", command="z", enabled=False)]
    deps = SimpleNamespace(
        store=store,
        workspace_switch=None,
        run_snapshot=lambda _sid: None,
        config_runtime=SimpleNamespace(config=config),
    )
    agent = SimpleNamespace(
        model=SimpleNamespace(model="gpt-x", name="openai"),
        plugins=[SettingsMCP(servers, book)],
    )
    ctx = RunContext(deps=None, entries=[], agent=agent, session_id="s1")
    token = RUN_SERVER.set(deps)  # type: ignore[arg-type]
    try:
        status = await lovia_status.invoke({}, ctx)
    finally:
        RUN_SERVER.reset(token)
    assert "- model of this reply: gpt-x (openai)" in status
    assert "- model setting: big — a change applies from the next message" in status
    assert "- fast: gpt-x (gpt-x, default)" in status
    assert "- big: Big (gpt-y)" in status
    # The book is shared by every chat and Settings' test: when, never whose.
    assert f"- files: on — connected {_when(200.0)}, 2 tools" in status
    assert f"- web: on — failed to connect {_when(50.0)}" in status
    assert "this run" not in status
    assert "- off: off for this chat" in status
    # A server's own words never reach the model.
    assert "secret" not in status


async def test_lovia_status_project_servers_not_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from lovia.web.introspection import lovia_status
    from lovia.web.mcp import MCPBook, SettingsMCP
    from lovia.web.project_mcp import ProjectMCP
    from lovia.web.run_deps import RUN_PROJECT, RUN_SERVER

    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("LOVIA_FAKE_SECRET", "sk-fake-1234")
    servers = {
        "db": {"command": "x"},
        "needs": {"command": "${NOT_SET_ANYWHERE}"},
        # The parser quotes a bad type after ${VAR} expansion.
        "leak": {"type": "${LOVIA_FAKE_SECRET}"},
    }
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    deps = SimpleNamespace(
        store=ChatStore.in_memory(),
        workspace_switch=None,
        run_snapshot=lambda _sid: None,
        config_runtime=None,
    )
    plugin = SettingsMCP([], MCPBook(), project=ProjectMCP(tmp_path / "home"))
    agent = SimpleNamespace(
        model=SimpleNamespace(model="m", name="p"), plugins=[plugin]
    )
    ctx = RunContext(deps=None, entries=[], agent=agent, session_id="s1")
    tokens = RUN_SERVER.set(deps), RUN_PROJECT.set(project.as_posix())  # type: ignore[arg-type]
    try:
        status = await lovia_status.invoke({}, ctx)
    finally:
        RUN_SERVER.reset(tokens[0])
        RUN_PROJECT.reset(tokens[1])
    # The page asks before a project's server starts; the tool says so.
    assert "none configured" not in status
    assert "- db: in .mcp.json, waiting for the user's OK on the page" in status
    for name in ("needs", "leak"):
        assert f"- {name}: in .mcp.json, can't be used (Settings → MCP says" in status
    # The parser's own words stay with Settings: they can quote secrets.
    assert "sk-fake-1234" not in status
    assert "NOT_SET_ANYWHERE" not in status


async def test_tools_need_the_web_app() -> None:
    ctx = RunContext(deps=None, entries=[], agent=SimpleNamespace(plugins=[]))
    for t, args in ((list_workspaces, {}), (read_chat, {"chat_id": "x"})):
        with pytest.raises(ToolError, match="web app"):
            await t.invoke(args, ctx)
