"""Background commands in the web layer: the watcher, the records finished
commands leave, and the routes that read both (``lovia.web.processes``)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import create_app, process_logs, processes  # noqa: E402
from lovia.web.api.deps import RouterDeps  # noqa: E402
from lovia.web.process_logs import (  # noqa: E402
    ProcessRecord,
    load_records,
    read_record,
    save_record,
)
from lovia.web.store import ChatStore  # noqa: E402
from lovia.workspace import RemoteHost, Workspace, WorkspaceLimits  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")

CHAT = uuid.UUID(int=7).hex


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(processes, "_EVERY", 0.05)


def _agent(root: Path, **kw) -> Agent:
    root.mkdir(exist_ok=True)
    return Agent(
        name="bot",
        model=None,
        workspace=Workspace.local(str(root), mode="trusted", **kw),
    )


class _World:
    """One chat's workspace session under a running watcher."""

    def __init__(self, deps: RouterDeps, agent: Agent) -> None:
        self.deps = deps
        self.agent = agent
        self.events: list[dict] = []
        real = deps.emit

        def emit(event, data):
            if event == "processes_changed":
                self.events.append(dict(data))
            real(event, data)

        deps.emit = emit  # type: ignore[method-assign]

    @property
    def folder(self) -> Path:
        folder = self.deps.store.process_log_folder(CHAT)
        assert folder is not None
        return folder

    async def session(self):
        await self.deps.workspaces.bind(CHAT, self.agent)
        session = self.deps.workspaces.get(CHAT)
        assert session is not None
        return session

    async def spawn(self, command: str) -> str:
        started = await (await self.session()).spawn(command)
        self.deps.processes.mark(CHAT)
        return started.process_id

    async def info(self, process_id: str):
        (info,) = [
            p
            for p in await self.deps.processes.infos(CHAT)
            if p.process_id == process_id
        ]
        return info

    def record(self, process_id: str) -> tuple[ProcessRecord, str]:
        found = read_record(self.folder, process_id)
        assert found is not None
        return found


@asynccontextmanager
async def _world(tmp_path: Path, agent: Agent | None = None):
    deps = RouterDeps(agents={}, store=ChatStore.sqlite(tmp_path / "data" / "lovia.db"))
    world = _World(deps, agent or _agent(tmp_path / "ws"))
    watcher = asyncio.create_task(deps.processes.run())
    try:
        yield world
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await deps.workspaces.aclose(keep_records=False)


async def _until(check, timeout: float = 10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not (value := check()):
        assert asyncio.get_running_loop().time() < deadline, "not in time"
        await asyncio.sleep(0.02)
    return value


# ------------------------------------------------------------- the watcher -


async def test_a_finished_command_leaves_a_record(tmp_path) -> None:
    async with _world(tmp_path) as w:
        pid = await w.spawn("echo 日志🎉; sleep 0.2; exit 3")
        await _until(lambda: w.events)
        assert w.events == [{"session_id": CHAT, "running": 1}]
        assert w.deps.processes.running(CHAT) == 1
        await _until(lambda: load_records(w.folder))
        assert w.events[1:] == [{"session_id": CHAT, "running": 0}]
        assert w.deps.processes.running(CHAT) == 0

        record, body = w.record(pid)
        assert body == "日志🎉\n"
        assert (record.status, record.exit_code, record.ended_by) == (
            "exited",
            3,
            "exit",
        )
        assert record.cwd == (tmp_path / "ws").resolve().as_posix()
        assert record.ended_at - record.started_at > 0.1
        assert (record.base, record.chars) == (0, 4)
        # The model's own reads are untouched by any of it.
        out = await (await w.session()).read_process_output(pid)
        assert out.output == "日志🎉\n"


async def test_a_file_is_one_json_line_and_the_output_as_it_was(tmp_path) -> None:
    async with _world(tmp_path) as w:
        pid = await w.spawn("printf 'a\\r\\nb\\033[0m'")
        await _until(lambda: load_records(w.folder))
        raw = (w.folder / f"{pid}.log").read_bytes()
        head, _, body = raw.partition(b"\n")
        assert json.loads(head)["id"] == pid
        assert body == b"a\r\nb\x1b[0m"
        assert w.folder.parent == tmp_path / "data" / "process-logs"


async def test_what_a_descendant_writes_after_the_exit_is_in_the_record(
    tmp_path,
) -> None:
    async with _world(tmp_path) as w:
        pid = await w.spawn("(sleep 0.6; echo late) & echo early")
        info = await w.info(pid)
        while info.status == "running":
            await asyncio.sleep(0.02)
            info = await w.info(pid)
        await asyncio.sleep(0.2)
        # Over, but its output is not: no record yet, and still readable.
        assert load_records(w.folder) == []
        page = await w.deps.processes.page(CHAT, pid, after=0, limit=100)
        assert (page.text, page.eof) == ("early\n", False)
        await _until(lambda: load_records(w.folder))
        assert w.record(pid)[1] == "early\nlate\n"


async def test_a_record_keeps_the_whole_buffer_and_its_offsets(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(processes, "OUTPUT_PAGE_CHARS", 1000)
    agent = _agent(
        tmp_path / "ws", limits=WorkspaceLimits(max_process_buffer_chars=5000)
    )
    async with _world(tmp_path, agent) as w:
        pid = await w.spawn("seq 1 5000")
        await _until(lambda: load_records(w.folder))
        whole = "".join(f"{i}\n" for i in range(1, 5001))
        record, body = w.record(pid)
        assert body == whole[-5000:]
        assert (record.base, record.chars) == (len(whole) - 5000, 5000)
        page = await w.deps.processes.page(CHAT, pid, after=0, limit=100)
        assert (page.base, page.next) == (record.base, record.base + 100)
        assert page.text == body[:100] and not page.eof
        rest = await w.deps.processes.page(CHAT, pid, after=page.next, limit=10**6)
        assert rest.text == body[100:] and rest.eof


async def test_a_title_that_comes_after_the_end_is_still_shown(tmp_path) -> None:
    async with _world(tmp_path) as w:
        pid = await w.spawn("true")
        await _until(lambda: load_records(w.folder))
        assert w.record(pid)[0].title == ""
        seen = len(w.events)
        w.deps.processes.mark(CHAT, started=pid, title="Check")
        assert len(w.events) == seen + 1  # the list is told to look again
        assert (await w.info(pid)).title == "Check"


async def test_who_stopped_a_command(tmp_path) -> None:
    async with _world(tmp_path) as w:
        by_user = await w.spawn("sleep 30")
        by_model = await w.spawn("sleep 30")
        await w.deps.processes.stop(CHAT, by_user)
        # At once, not at the next look.
        assert (await w.info(by_user)).ended_by == "user"
        assert w.events[-1] == {"session_id": CHAT, "running": 1}
        await (await w.session()).kill_process(by_model)
        await _until(lambda: len(load_records(w.folder)) == 2)
        assert w.record(by_user)[0].ended_by == "user"
        assert w.record(by_model)[0].ended_by == "model"
        # The user's stop is the model's to read; a record is nobody's read.
        statuses = {
            s.process_id: s.exit_seen
            for s in (await w.session()).background_processes()
        }
        assert statuses == {by_user: False, by_model: True}


async def test_nothing_running_is_not_looked_at(tmp_path) -> None:
    async with _world(tmp_path) as w:
        await w.spawn("echo done")
        await _until(lambda: load_records(w.folder))
        session = await w.session()
        looks = 0
        real = session.background_processes

        def counting():
            nonlocal looks
            looks += 1
            return real()

        session.background_processes = counting
        await asyncio.sleep(0.4)
        looks = 0
        await asyncio.sleep(0.4)
        assert looks == 0
        w.deps.processes.mark(CHAT)
        await _until(lambda: looks)


async def test_a_host_slower_than_the_watcher_is_still_watched(tmp_path) -> None:
    async with _world(tmp_path) as w:
        session = await w.session()
        real = session.background_processes

        async def slow():
            await asyncio.sleep(0.2)  # several looks' worth
            return real()

        session.background_processes = slow
        pid = await w.spawn("sleep 0.5; echo done")
        await _until(lambda: load_records(w.folder))
        assert w.record(pid)[1] == "done\n"


async def test_a_chat_whose_id_cannot_name_a_folder_keeps_no_records(tmp_path) -> None:
    async with _world(tmp_path) as w:
        await w.deps.workspaces.bind("../evil", w.agent)
        session = w.deps.workspaces.get("../evil")
        pid = (await session.spawn("echo hi")).process_id
        w.deps.processes.mark("../evil")
        await _until(lambda: len(w.events) >= 1)
        await asyncio.sleep(0.3)
        (info,) = await w.deps.processes.infos("../evil")
        assert (info.process_id, info.status) == (pid, "exited")
        assert not (tmp_path / "data" / "process-logs").exists()


# ----------------------------------------------------------------- closing -


async def test_shutdown_records_a_running_command_with_its_output(tmp_path) -> None:
    async with _world(tmp_path) as w:
        pid = await w.spawn("echo serving; echo $$ > pid; exec sleep 30")
        await _until((tmp_path / "ws" / "pid").exists)
        os_pid = int((tmp_path / "ws" / "pid").read_text())
        await w.deps.workspaces.aclose()
        record, body = w.record(pid)
        assert body == "serving\n"
        assert (record.status, record.ended_by) == ("killed", "shutdown")
        with pytest.raises(ProcessLookupError):
            os.kill(os_pid, 0)
        assert w.events[-1] == {"session_id": CHAT, "running": 0}
        # Read from the record from here on.
        (info,) = await w.deps.processes.infos(CHAT)
        assert (info.status, info.ended_by) == ("killed", "shutdown")


async def test_a_moved_chat_keeps_the_records_of_the_old_directory(tmp_path) -> None:
    async with _world(tmp_path) as w:
        done = await w.spawn("echo one")
        await _until(lambda: load_records(w.folder))
        live = await w.spawn("echo two; sleep 30")
        await asyncio.sleep(0.2)
        w.agent = _agent(tmp_path / "other")
        new = await w.spawn("sleep 30")
        assert w.record(done)[0].ended_by == "exit"
        record, body = w.record(live)
        assert (record.ended_by, body) == ("closed", "two\n")
        assert [p.process_id for p in await w.deps.processes.infos(CHAT)] == [
            done,
            live,
            new,
        ]


async def test_closing_kills_what_an_exited_command_left_behind(tmp_path) -> None:
    async with _world(tmp_path) as w:
        pid = await w.spawn("sleep 30 & echo early")
        await _until(lambda: any(e["running"] == 0 for e in w.events))
        await w.deps.workspaces.close(CHAT)
        record, body = w.record(pid)
        assert (record.status, record.ended_by, body) == ("exited", "exit", "early\n")


async def test_closing_out_of_time_keeps_what_it_fetched(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(processes, "_CLOSE_TIMEOUT", 0.5)
    async with _world(tmp_path) as w:
        pid = await w.spawn("echo early; sleep 30")
        session = await w.session()
        await _until(lambda: session.background_processes()[0].output_chars)

        async def stuck(process_id):
            await asyncio.sleep(30)

        session.stop_process = stuck
        start = time.monotonic()
        await w.deps.workspaces.close(CHAT)
        assert time.monotonic() - start < 5
        record, body = w.record(pid)
        assert (record.status, record.ended_by, body) == ("killed", "closed", "early\n")


async def test_deleting_the_chat_leaves_nothing(tmp_path) -> None:
    async with _world(tmp_path) as w:
        await w.deps.store.upsert(CHAT, title="t")
        await w.spawn("echo one")
        await _until(lambda: load_records(w.folder))
        await w.spawn("echo two; sleep 30")
        await w.deps.workspaces.close(CHAT, keep_records=False)
        await w.deps.store.delete(CHAT)
        assert not w.folder.exists()
        assert await w.deps.processes.infos(CHAT) == []


async def test_a_dropped_connection_leaves_the_record_without_output(tmp_path) -> None:
    host = RemoteHost("far", command=(), python=f"{sys.executable} -I -S")
    agent = replace(
        _agent(tmp_path / "ws"),
        workspace=Workspace.remote(host, str(tmp_path / "ws"), mode="trusted"),
    )
    try:
        async with _world(tmp_path, agent) as w:
            done = await w.spawn("echo one")
            await _until(lambda: load_records(w.folder))
            live = await w.spawn("echo two; sleep 30")
            watch = w.deps.processes._watches[CHAT]
            await _until(lambda: live in watch.known and watch.known[live].output_chars)
            os.kill(host._conn.ready["pid"], 9)
            await _until(lambda: len(load_records(w.folder)) == 2)
            assert w.record(done)[1] == "one\n"
            record, body = w.record(live)
            assert (record.status, record.ended_by, body) == ("killed", "lost", "")
            # A reader that had got that far does not go back to 0.
            assert record.base == len("two\n")
            # The announcement follows the records' writes (_wind_up): the
            # files can be on disk a moment before it is made.
            await _until(lambda: w.events[-1] == {"session_id": CHAT, "running": 0})
            # Asking again, as a page told of the change does, changes nothing.
            heard = len(w.events)
            for _ in range(3):
                assert len(await w.deps.processes.infos(CHAT)) == 2
                await asyncio.sleep(0.1)
            assert len(w.events) == heard
    finally:
        await host.aclose()


async def test_a_remote_hosts_times_are_shown_by_this_clock(tmp_path) -> None:
    host = RemoteHost("far", command=(), python=f"{sys.executable} -I -S")
    agent = replace(
        _agent(tmp_path / "ws"),
        workspace=Workspace.remote(host, str(tmp_path / "ws"), mode="trusted"),
    )
    try:
        async with _world(tmp_path, agent) as w:
            session = await w.session()
            real = session.background_processes

            async def an_hour_behind():
                return [
                    replace(
                        s,
                        started_at=s.started_at - 3600,
                        ended_at=s.ended_at and s.ended_at - 3600,
                    )
                    for s in await real()
                ]

            session.background_processes = an_hour_behind
            pid = await w.spawn("sleep 0.3")
            await _until(lambda: load_records(w.folder))
            record = w.record(pid)[0]
            assert abs(record.started_at - time.time()) < 60
            assert 0.2 < record.ended_at - record.started_at < 5
    finally:
        await host.aclose()


# ----------------------------------------------------------------- records -


def _record(process_id: str, **kw) -> ProcessRecord:
    return ProcessRecord(
        **{
            "id": process_id,
            "command": "echo",
            "cwd": "/",
            "started_at": 1.0,
            "ended_at": 2.0,
            "status": "exited",
            "exit_code": 0,
            "ended_by": "exit",
            "base": 0,
            "chars": 1,
            **kw,
        }
    )


def test_a_chat_keeps_its_newest_records(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(process_logs, "_MAX_RECORDS", 3)
    for i in range(5):
        save_record(tmp_path, _record(f"bg-{i}"), "x")
        os.utime(tmp_path / f"bg-{i}.log", (i + 1, i + 1))
    assert sorted(r.id for r in load_records(tmp_path)) == ["bg-2", "bg-3", "bg-4"]


def test_a_record_is_one_line_whatever_the_command(tmp_path) -> None:
    command = "echo 'a\nb c'\n" + "x" * 10_000
    save_record(tmp_path, _record("bg-1", command=command), "out\n")
    record, body = read_record(tmp_path, "bg-1")
    assert record.command == command[: process_logs._MAX_COMMAND_CHARS]
    assert body == "out\n"


def test_a_record_from_before_titles_still_loads(tmp_path) -> None:
    save_record(tmp_path, _record("bg-1", title="Build"), "x")
    path = tmp_path / "bg-1.log"
    head, _, body = path.read_text().partition("\n")
    data = json.loads(head)
    assert data.pop("title") == "Build"
    path.write_text(json.dumps(data) + "\n" + body)
    (record,) = load_records(tmp_path)
    assert (record.id, record.title) == ("bg-1", "")


def test_strangers_files_and_names_are_not_records(tmp_path) -> None:
    (tmp_path / "notes.log").write_text("just text\n")
    (tmp_path / "half.log").write_text('{"id": "half"}\n')
    (tmp_path / "empty.log").write_text("")
    assert load_records(tmp_path) == []
    for name in ("notes", "half", "empty", "../x", "a.b", ""):
        assert read_record(tmp_path, name) is None
    save_record(tmp_path / "sub", _record("../up"), "x")
    assert not (tmp_path / "up.log").exists()
    assert load_records(tmp_path / "missing") == []


async def test_the_sweep_trims_the_oldest_and_drops_orphans(tmp_path) -> None:
    store = ChatStore.sqlite(tmp_path / "chat.db")
    chat, other, orphan = (uuid.uuid4().hex for _ in range(3))
    for sid in (chat, other):
        await store.upsert(sid, title="t")
    for i, sid in enumerate((chat, other, chat)):
        folder = store.process_log_folder(sid)
        save_record(folder, _record(f"bg-{i}"), "x" * 1000)
        os.utime(folder / f"bg-{i}.log", (i + 1, i + 1))
    root = store.process_log_folder(chat).parent
    await store.make_folder(root / orphan)
    for name in (orphan, "notes"):
        save_record(root / name, _record("bg-9"), "x")
        os.utime(root / name, (1, 1))
    await store.sweep_process_logs(max_age=60, max_bytes=2800)
    assert sorted(p.name for p in root.iterdir()) == sorted([chat, other, "notes"])
    assert [r.id for r in load_records(root / chat)] == ["bg-2"]
    assert [r.id for r in load_records(root / other)] == ["bg-1"]
    assert (root / "notes" / "bg-9.log").exists()


async def test_another_stores_records_are_neither_trimmed_nor_dropped(
    tmp_path,
) -> None:
    a = ChatStore.sqlite(tmp_path / "a.db")
    b = ChatStore.sqlite(tmp_path / "b.db")
    chat, gone = uuid.uuid4().hex, uuid.uuid4().hex
    await b.upsert(chat, title="t")
    for sid in (chat, gone):
        folder = b.process_log_folder(sid)
        await b.make_folder(folder)
        save_record(folder, _record("bg-1"), "x" * 1000)
        os.utime(folder, (1, 1))
    root = b.process_log_folder(chat).parent
    await a.sweep_process_logs(max_age=60, max_bytes=0)
    await a.delete(chat)  # not a's chat: its records are not a's to drop
    await a.delete_all()
    assert sorted(p.name for p in root.iterdir()) == sorted([chat, gone])
    assert [r.id for r in load_records(root / chat)] == ["bg-1"]
    await b.sweep_process_logs(max_age=60)
    assert [p.name for p in root.iterdir()] == [chat]


def test_the_total_cap_is_configurable(monkeypatch) -> None:
    monkeypatch.delenv("LOVIA_MAX_PROCESS_LOGS_MB", raising=False)
    assert process_logs.max_total_bytes() == 300 * 1024 * 1024
    monkeypatch.setenv("LOVIA_MAX_PROCESS_LOGS_MB", "5")
    assert process_logs.max_total_bytes() == 5 * 1024 * 1024
    monkeypatch.setenv("LOVIA_MAX_PROCESS_LOGS_MB", "lots")
    assert process_logs.max_total_bytes() == 300 * 1024 * 1024


async def test_an_in_memory_stores_records_go_with_it(tmp_path) -> None:
    store = ChatStore.in_memory()
    folder = store.process_log_folder(CHAT)
    save_record(folder, _record("bg-1"), "x")
    await store.upsert(CHAT, title="t")
    await store.delete(CHAT)
    assert not folder.exists()
    root = folder.parent
    assert root.is_dir()
    del store
    import gc

    gc.collect()
    assert not root.exists()


# --------------------------------------------------------------------- API -


def _served(tmp_path: Path, script: list) -> TestClient:
    agent = replace(_agent(tmp_path / "ws"), model=ScriptedProvider(script))
    return TestClient(create_app(agent, db_path=tmp_path / "lovia.db", titles=False))


def test_the_routes_before_and_after_a_restart(tmp_path) -> None:
    command = "echo serving; touch up; sleep 30"
    args = {"command": command, "background": True, "description": " Serve\n it "}
    script = [call("shell", args), text("ok")]
    with _served(tmp_path, script) as c:
        sid = c.post("/api/chat", json={"message": "go"}).json()["session_id"]
        deadline = time.time() + 5
        while not (tmp_path / "ws" / "up").exists() and time.time() < deadline:
            time.sleep(0.02)
        # A shell(background=true) is seen without anyone asking for the list.
        while time.time() < deadline:
            (row,) = c.get("/api/sessions").json()
            if row["processes_running"]:
                break
            time.sleep(0.02)
        assert row["processes_running"] == 1
        (live,) = c.get(f"/api/sessions/{sid}/processes").json()
        pid = live["process_id"]
        assert (live["status"], live["ended_by"]) == ("running", None)
        assert live["last_line"] == "serving"
        # What the model said it does, on one line.
        assert live["title"] == "Serve it"
        url = f"/api/sessions/{sid}/processes/{pid}"
        page = c.get(f"{url}/output", params={"after": 3, "limit": 4}).json()
        assert page == {"text": "ving", "base": 0, "next": 7, "eof": False}
        log = c.get(f"{url}/log")
        assert log.text == "serving\n"
        assert log.headers["content-type"] == "text/plain; charset=utf-8"
        assert f"{pid}.log" in log.headers["content-disposition"]
        assert c.get(f"{url}/output", params={"limit": 0}).status_code == 422

    with _served(tmp_path, []) as c:
        (past,) = c.get(f"/api/sessions/{sid}/processes").json()
        assert past["process_id"] == pid
        assert (past["status"], past["ended_by"]) == ("killed", "shutdown")
        assert (past["command"], past["title"]) == (command, "Serve it")
        page = c.get(f"{url}/output").json()
        assert page == {"text": "serving\n", "base": 0, "next": 8, "eof": True}
        assert c.get(f"{url}/log").text == "serving\n"
        (row,) = c.get("/api/sessions").json()
        assert row["processes_running"] == 0
        for path in ("output", "log"):
            missing = c.get(f"/api/sessions/{sid}/processes/nope/{path}")
            assert missing.status_code == 404
            assert missing.json()["detail"]["code"] == "process_not_found"
        # A command that is over cannot be stopped.
        assert c.post(f"{url}/kill").status_code == 404
        c.delete(f"/api/sessions/{sid}")
        assert c.get(f"/api/sessions/{sid}/processes").json() == []
    assert list((tmp_path / "process-logs").iterdir()) == []
