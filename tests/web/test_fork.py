"""Forking a chat (#306): copy its finished history into a new, independent
chat — whole, or up to just before one of its user messages."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from lovia import Agent  # noqa: E402
from lovia.checkpointer import RunHead  # noqa: E402
from lovia.messages import Usage  # noqa: E402
from lovia.stores import InMemorySession  # noqa: E402
from lovia.stores._sqlite import SQLiteStore  # noqa: E402
from lovia.transcript import (  # noqa: E402
    AssistantTextEntry,
    InputEntry,
    ToolCallEntry,
    ToolResultEntry,
)
from lovia.web.store import _META_SCHEMA, _MIGRATIONS, ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_supervisor import _blocking_tool, _client, _spawn, _wait_run  # noqa: E402
from .test_web import _app  # noqa: E402


def _user(text_: str) -> InputEntry:
    return InputEntry(role="user", content=text_)


def _reply(text_: str) -> AssistantTextEntry:
    return AssistantTextEntry(content=text_)


async def _chat(store: ChatStore, sid: str = "src") -> None:
    """Two finished runs; the second took a mid-run (injected) message."""
    await store.upsert(sid, agent="bot", title="Source")
    await store.session.append(
        sid, [_user("q1"), _reply("a1")], run_id="r1", meta={"context_state": {"v": 1}}
    )
    await store.session.append(
        sid,
        [
            _user("q2"),
            ToolCallEntry(call_id="c1", name="t", arguments="{}"),
            ToolResultEntry(call_id="c1", output="ok"),
            _user("steer"),
            _reply("a2"),
        ],
        run_id="r2",
        meta={"context_state": {"v": 2}},
    )


def _error(res) -> tuple[int, str]:
    return res.status_code, res.json()["detail"]["code"]


def _texts(entries) -> list[str]:
    return [
        e.content for e in entries if isinstance(e, (InputEntry, AssistantTextEntry))
    ]


# ---------------------------------------------------------------- the store -


async def test_a_whole_fork_copies_runs_meta_and_ui_data() -> None:
    store = ChatStore.in_memory()
    await _chat(store)
    data_ref = await store.put_ui_data("src", b'{"rows": [1]}')
    assert data_ref is not None

    fork = await store.fork("src")

    assert fork is not None and fork != "src"
    assert await store.session.segments(fork) == await store.session.segments("src")
    meta = await store.get(fork)
    assert meta is not None
    assert (meta.title, meta.agent, meta.parent_id) == ("Source", "bot", None)
    assert (meta.origin_kind, meta.origin_session_id, meta.origin_ref) == (
        "fork",
        "src",
        None,
    )
    # The same ref resolves in the copy: the transcript needs no rewriting.
    assert await store.get_ui_data(fork, data_ref) == b'{"rows": [1]}'


async def test_a_fork_at_a_run_boundary_keeps_whole_runs() -> None:
    store = ChatStore.in_memory()
    await _chat(store)

    fork = await store.fork("src", ref="r2:0")

    assert fork is not None
    segments = await store.session.segments(fork)
    assert [(s.run_id, _texts(s.entries), s.meta) for s in segments] == [
        ("r1", ["q1", "a1"], {"context_state": {"v": 1}})
    ]


async def test_a_fork_inside_a_run_cuts_it_like_a_rewind() -> None:
    store = ChatStore.in_memory()
    await _chat(store)

    fork = await store.fork("src", ref="r2:1")  # before the injected "steer"

    assert fork is not None
    last = (await store.session.segments(fork))[-1]
    assert last.run_id == "r2" and _texts(last.entries) == ["q2"]
    # The cut run's carried context state describes the dropped tail: gone.
    assert last.meta is None
    # The tool pair before the cut stays whole.
    assert [type(e).__name__ for e in last.entries] == [
        "InputEntry",
        "ToolCallEntry",
        "ToolResultEntry",
    ]


async def test_the_source_is_untouched() -> None:
    store = ChatStore.in_memory()
    await _chat(store)
    before = await store.session.segments("src")

    await store.fork("src", ref="r2:1")

    assert await store.session.segments("src") == before
    assert (await store.get("src")).origin_kind is None  # type: ignore[union-attr]


@pytest.mark.parametrize("ref", ["r9:0", "r1:5", "r1", "r1:x", ":"])
async def test_an_unknown_ref_is_refused(ref: str) -> None:
    store = ChatStore.in_memory()
    await _chat(store)

    with pytest.raises(ValueError):
        await store.fork("src", ref=ref)


async def test_nothing_to_copy_is_none() -> None:
    store = ChatStore.in_memory()
    await _chat(store)
    await store.upsert("empty")

    assert await store.fork("src", ref="r1:0") is None  # before the first message
    assert await store.fork("empty") is None


async def test_deleting_either_chat_leaves_the_other() -> None:
    store = ChatStore.in_memory()
    await _chat(store)
    data_ref = await store.put_ui_data("src", b"{}")
    fork = await store.fork("src")
    assert fork is not None and data_ref is not None

    await store.delete("src")

    assert await store.get(fork) is not None
    assert _texts(await store.session.load(fork))[-1] == "a2"
    assert await store.get_ui_data(fork, data_ref) == b"{}"
    # The fork still names its source, which is simply gone.
    assert (await store.get(fork)).origin_session_id == "src"  # type: ignore[union-attr]

    again = await store.fork(fork)
    assert again is not None
    await store.delete(fork)
    assert await store.get(again) is not None


async def test_a_source_deleted_mid_fork_publishes_nothing(monkeypatch) -> None:
    store = ChatStore.in_memory()
    await _chat(store)
    session = store.session
    read = session.segments

    async def read_then_delete(session_id: str):
        segments = await read(session_id)
        await store.delete("src")  # lands between the snapshot and the publish
        return segments

    monkeypatch.setattr(session, "segments", read_then_delete)
    with pytest.raises(LookupError):
        await store.fork("src")
    monkeypatch.undo()

    assert isinstance(session, InMemorySession)
    assert [sid for sid in session._segments if session._segments[sid]] == []
    assert await store.list() == []


async def test_a_failed_copy_clears_what_it_wrote(monkeypatch) -> None:
    store = ChatStore.in_memory()
    await _chat(store)
    session = store.session
    append = session.append
    written: list[str] = []

    async def fail_second(session_id: str, entries, **kw):
        written.append(session_id)
        if len(written) == 2:
            raise OSError("disk full")
        return await append(session_id, entries, **kw)

    monkeypatch.setattr(session, "append", fail_second)
    with pytest.raises(OSError):
        await store.fork("src")
    monkeypatch.undo()

    assert await session.segments(written[0]) == []
    assert [m.id for m in await store.list()] == ["src"]


async def test_a_v1_database_migrates_its_ui_data(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    # A database as the version-1 chat store left it.
    v1 = SQLiteStore(str(path), _META_SCHEMA, name="chat", migrations=_MIGRATIONS[:1])
    with v1._tx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, created_at, updated_at) VALUES ('s', 1, 1)"
        )
        conn.execute(
            "INSERT INTO chat_ui_data (id, session_id, created_at, payload) "
            "VALUES ('d1', 's', 1, ?)",
            (__import__("zlib").compress(b'{"a": 1}'),),
        )

    store = ChatStore(InMemorySession(), meta_path=path)

    assert await store.get_ui_data("s", "d1") == b'{"a": 1}'
    conn = sqlite3.connect(path)
    pk = [r[1] for r in conn.execute("PRAGMA table_info(chat_ui_data)") if r[5]]
    version = conn.execute(
        "SELECT version FROM lovia_schema WHERE store = 'chat'"
    ).fetchone()[0]
    conn.close()
    assert pk == ["session_id", "id"] and version == len(_MIGRATIONS)


# ---------------------------------------------------------------- the route -


async def test_fork_route_cuts_before_the_message_and_links_back() -> None:
    store = ChatStore.in_memory()
    await _chat(store)
    app = _app(Agent(name="bot", model=ScriptedProvider([text("a3")])), store=store)
    async with _client(app) as ac:
        res = await ac.post("/api/sessions/src/fork", json={"ref": "r2:0"})
        assert res.status_code == 200
        fork = res.json()["session_id"]

        detail = (await ac.get(f"/api/sessions/{fork}")).json()
        assert [m["content"] for m in detail["entries"]] == ["q1", "a1"]
        assert detail["origin"] == {
            "kind": "fork",
            "session_id": "src",
            "ref": "r2:0",
            "title": "Source",
            "exists": True,
        }
        assert (await ac.get("/api/sessions/src")).json()["origin"] is None

        # The fork lists as an ordinary chat, marked as a branch, and runs on
        # its own.
        listed = {s["id"]: s for s in (await ac.get("/api/sessions")).json()}
        assert listed[fork]["origin_kind"] == "fork"
        assert listed["src"]["origin_kind"] is None
        res = await ac.post("/api/chat", json={"message": "q3", "session_id": fork})
        assert res.status_code == 200
        assert _texts(await store.session.load("src"))[-1] == "a2"

        await ac.delete("/api/sessions/src")
        origin = (await ac.get(f"/api/sessions/{fork}")).json()["origin"]
        assert (origin["exists"], origin["title"]) == (False, None)


async def test_fork_route_errors() -> None:
    store = ChatStore.in_memory()
    await _chat(store)
    await store.upsert("empty")
    app = _app(Agent(name="bot", model=ScriptedProvider([])), store=store)
    async with _client(app) as ac:
        res = await ac.post("/api/sessions/nope/fork", json={})
        assert _error(res) == (404, "session_not_found")
        res = await ac.post("/api/sessions/src/fork", json={"ref": "r9:0"})
        assert _error(res) == (404, "turn_not_found")
        res = await ac.post("/api/sessions/src/fork", json={"ref": "r1:0"})
        assert _error(res) == (409, "nothing_to_fork")
        res = await ac.post("/api/sessions/empty/fork", json={})
        assert _error(res) == (409, "nothing_to_fork")


async def test_a_whole_copy_waits_for_a_resumable_run() -> None:
    store = ChatStore.in_memory()
    await _chat(store)
    assert store.checkpointer is not None
    await store.checkpointer.append(
        "r3",
        [_user("q3")],
        RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
    )
    await store.set_active_run_id("src", "r3")
    app = _app(Agent(name="bot", model=ScriptedProvider([])), store=store)
    async with _client(app) as ac:
        res = await ac.post("/api/sessions/src/fork", json={})
        assert _error(res) == (409, "run_unfinished")
        # A fork from a finished message needs nothing from that run.
        res = await ac.post("/api/sessions/src/fork", json={"ref": "r2:0"})
        assert res.status_code == 200
    # Neither touched the checkpoint.
    assert await store.get_active_run_id("src") == "r3"
    assert await store.checkpointer.load("r3") is not None


async def test_a_whole_copy_waits_for_a_live_run() -> None:
    release = asyncio.Event()
    agent = Agent(
        name="bot",
        model=ScriptedProvider([text("a1"), text("a2"), call("block", {}), text("a3")]),
        tools=[_blocking_tool(release)],
    )
    store = ChatStore.in_memory()
    app = _app(agent, store=store)
    async with _client(app) as ac:
        for message in ("q1", "q2"):
            res = await ac.post(
                "/api/chat", json={"message": message, "session_id": "s"}
            )
            assert res.status_code == 200
        entries = (await ac.get("/api/sessions/s")).json()["entries"]
        q2 = [m["ref"] for m in entries if m["role"] == "user"][1]

        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "q3", "session_id": "s"}
        )
        await _wait_run(ac, "s")
        res = await ac.post("/api/sessions/s/fork", json={})
        assert _error(res) == (409, "run_active")
        # A finished message forks while the run goes on.
        res = await ac.post("/api/sessions/s/fork", json={"ref": q2})
        assert res.status_code == 200
        fork = res.json()["session_id"]
        assert _texts(await store.session.load(fork)) == ["q1", "a1"]
        release.set()
        await task
        await _wait_run(ac, "s", gone=True)
        res = await ac.post("/api/sessions/s/fork", json={})
        assert res.status_code == 200
        whole = res.json()["session_id"]
        assert _texts(await store.session.load(whole))[-1] == "a3"
