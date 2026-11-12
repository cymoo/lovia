"""Remote workspaces, over the one transport every machine has: a subprocess.

The far side is this interpreter started with ``-I -S`` — no site-packages —
so each test also proves the shipped engine runs on the standard library
alone, through the real bootstrap. SSH and containers differ only in the
command that reaches the far side; what is tested here is everything after.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import signal
import sys

import pytest

from lovia import Agent, Runner
from lovia.exceptions import UserError
from lovia.workspace import (
    FileTooLargeError,
    PermissionDeniedError,
    RemoteHost,
    RemoteWorkspace,
    Workspace,
    WorkspaceClosedError,
    WorkspaceDisconnectedError,
    WorkspaceError,
    WorkspacePolicy,
)
from lovia.workspace.remote import host as host_module
from lovia.workspace.remote.wire import decode, decode_error, encode, encode_error

from ..scripted_provider import ScriptedProvider, call, text

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX far side")


def _host(name: str = "far") -> RemoteHost:
    return RemoteHost(name, command=(), python=f"{sys.executable} -I -S")


@pytest.fixture
async def host():
    h = _host()
    yield h
    await h.aclose()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def _dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


def test_a_system_error_survives_the_wire_as_itself() -> None:
    # What the far side's system said arrives as the same error: its class
    # (picked by errno), errno and text — not a nameless WorkspaceError.
    full = decode_error(encode_error(OSError(errno.ENOSPC, "No space left", "/x/a")))
    assert type(full) is OSError
    assert (full.errno, full.filename) == (errno.ENOSPC, "/x/a")
    assert str(full) == str(OSError(errno.ENOSPC, "No space left", "/x/a"))
    denied = decode_error(
        encode_error(PermissionError(errno.EACCES, "Permission denied"))
    )
    assert type(denied) is PermissionError
    # It travels by name: errno numbers differ between platforms (Linux's
    # ENAMETOOLONG is another error's number on macOS), and one this side
    # has no name for stays a WorkspaceError rather than a wrong class.
    sent = encode_error(OSError(errno.ENAMETOOLONG, "File name too long"))
    assert sent["os"][0] == "ENAMETOOLONG"
    sent["os"][0] = "ENOSUCHERRNO"
    assert type(decode_error(sent)) is WorkspaceError
    # One the engine raised by hand has no errno and keeps its message.
    plain = decode_error(encode_error(FileNotFoundError("Not a file: a.txt")))
    assert type(plain) is FileNotFoundError and str(plain) == "Not a file: a.txt"


def test_policy_survives_the_wire_unchanged() -> None:
    policy = Workspace.local(
        ".", mode="coding", readable=("~/docs",), denied_paths=("secrets/**",)
    ).policy
    assert decode(encode(policy)) == policy


def test_a_policy_with_a_callable_cannot_travel() -> None:
    policy = WorkspacePolicy(command_decider=lambda command: "allow")
    with pytest.raises(UserError, match="command_decider"):
        encode(policy)


# ---------------------------------------------------------------------------
# The session, end to end
# ---------------------------------------------------------------------------


async def test_files_round_trip(host, tmp_path) -> None:
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    assert session.root == tmp_path.resolve().as_posix()

    text_in = "héllo — 你好\nline two\r\n"
    assert (await session.write_text("a/note.txt", text_in)).action == "created"
    assert (await session.read_text("a/note.txt")).content == text_in
    assert (tmp_path / "a" / "note.txt").read_bytes() == text_in.encode()

    blob = bytes(range(256)) * 400
    await session.write_bytes("blob.bin", blob)
    assert (await session.read_bytes("blob.bin")).data == blob

    # … or a piece of it, and what it is without any of it.
    piece = await session.read_bytes("blob.bin", offset=250, length=12)
    assert piece.data == blob[250:262]
    entry = await session.stat("blob.bin")
    assert (entry.path, entry.is_dir, entry.size) == ("blob.bin", False, len(blob))
    assert entry.mtime == (tmp_path / "blob.bin").stat().st_mtime
    with pytest.raises(FileNotFoundError):
        await session.stat("nope.bin")

    edit = await session.edit_text("a/note.txt", "line two", "line 2")
    assert edit.ok and edit.replacements == 1
    assert [m.line for m in await session.grep("line 2")] == [2]

    listing = await session.list_files(".", pattern="**/*")
    assert sorted(e.path for e in listing) == ["a", "a/note.txt", "blob.bin"]
    assert listing.truncated is False  # the flag survives the trip
    capped = await session.list_files(".", pattern="**/*", max_results=1)
    assert len(capped) == 1 and capped.truncated is True


async def test_a_request_too_large_to_send_is_refused_not_fatal(
    host, tmp_path, monkeypatch
) -> None:
    # Past the line limit the far side would hang up — on every session of
    # every chat there. The near side refuses to send it instead.
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    monkeypatch.setattr(host_module, "MAX_LINE", 4096)
    with pytest.raises(FileTooLargeError, match="can be sent at once"):
        await session.write_bytes("big.bin", b"x" * 8192)
    assert not (tmp_path / "big.bin").exists()
    # The connection, and the session on it, are none the worse.
    assert (await session.write_bytes("ok.bin", b"x" * 16)).ok
    assert host._conn is not None and host._conn.lost is None


async def test_a_close_whose_caller_is_cancelled_still_closes(host, tmp_path) -> None:
    # A request whose client went away closes its session from inside a
    # cancelled scope (anyio's stay cancelled: every await in them raises).
    # The far side must still close it — and end what it started.
    anyio = pytest.importorskip("anyio")
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    started = await session.spawn("sleep 30")
    conn = host._conn
    assert conn is not None
    told: list[str] = []
    notify = conn.notify
    conn.notify = lambda method, *a, **k: (
        told.append(method),
        notify(method, *a, **k),
    )[1]  # type: ignore[method-assign]
    with anyio.CancelScope() as scope:
        scope.cancel()
        await session.close()
    assert scope.cancelled_caught and session.closed
    # The far side was never told to drop the close …
    assert told == []
    # … so the session is gone there, and its process with it.
    with pytest.raises(WorkspaceClosedError):
        await conn.call("background_processes", session.id, {})
    assert await _dead(started.pid)


async def test_commands_and_background_processes(host, tmp_path) -> None:
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    result = await session.run("echo out; echo err >&2; exit 3")
    assert (result.exit_code, result.stdout, result.stderr) == (3, "out\n", "err\n")
    # A command reading stdin gets EOF, not the protocol.
    assert (await session.run("cat; echo done", timeout=5)).stdout == "done\n"

    started = await session.spawn("sleep 30")
    assert [p.status for p in await session.background_processes()] == ["running"]
    assert (await session.kill_process(started.process_id)).status == "killed"
    assert await _dead(started.pid)


async def test_errors_arrive_as_the_same_exceptions(host, tmp_path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * 100)
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.readonly())
    with pytest.raises(PermissionDeniedError):
        await session.write_text("x.txt", "x")
    with pytest.raises(FileTooLargeError):
        await session.read_bytes("big.bin", max_bytes=10)
    with pytest.raises(WorkspaceError, match="Not a file") as caught:
        await session.read_text("nope.txt")
    assert type(caught.value) is WorkspaceError
    with pytest.raises(UserError, match="does not exist"):
        await host.open(str(tmp_path / "missing"), policy=WorkspacePolicy.coding())


async def test_policy_is_judged_where_the_files_are(host, tmp_path) -> None:
    outside = tmp_path / "outside"
    (tmp_path / "root").mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    (tmp_path / "root" / "link.txt").symlink_to(outside / "secret.txt")
    session = await host.open(str(tmp_path / "root"), policy=WorkspacePolicy.coding())

    assert (await session.decide_path("plain.txt")).action == "allow"
    # The symlink is followed over there: it leads outside the root.
    assert (await session.decide_path("link.txt")).action == "ask"
    assert (await session.decide_command("git push --force")).action == "deny"

    # A mode switch takes effect for the very next request.
    session.set_policy(WorkspacePolicy.readonly())
    assert (await session.decide_path("plain.txt", write=True)).action == "deny"
    with pytest.raises(PermissionDeniedError):
        await session.write_text("plain.txt", "x")


async def test_one_connection_carries_many_sessions(host, tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = await host.open(str(tmp_path / "a"), policy=WorkspacePolicy.trusted())
    b = await host.open(str(tmp_path / "b"), policy=WorkspacePolicy.trusted())
    assert a._conn is b._conn

    started = await a.spawn("sleep 30")
    await a.close()  # closing one session ends its processes only
    assert await _dead(started.pid)
    assert (await b.run("echo still here")).stdout == "still here\n"
    with pytest.raises(WorkspaceError, match="closed"):
        await a.read_text("x")
    assert await a.background_processes() == []


# ---------------------------------------------------------------------------
# Cancel and disconnect
# ---------------------------------------------------------------------------


async def test_cancelling_a_call_kills_what_it_started(host, tmp_path) -> None:
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    task = asyncio.create_task(session.run("echo $$ > pid; exec sleep 30"))
    while not (tmp_path / "pid").exists() or not (tmp_path / "pid").read_text():
        await asyncio.sleep(0.02)
    pid = int((tmp_path / "pid").read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _dead(pid)
    # The session and the connection are none the worse.
    assert (await session.run("echo ok")).stdout == "ok\n"


async def test_a_lost_connection_fails_calls_and_cleans_up(host, tmp_path) -> None:
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    started = await session.spawn("sleep 30")
    in_flight = asyncio.create_task(session.run("echo $$ > pid; exec sleep 30"))
    while not (tmp_path / "pid").exists() or not (tmp_path / "pid").read_text():
        await asyncio.sleep(0.02)

    host._conn._proc.kill()  # the far side dies under us
    with pytest.raises(WorkspaceDisconnectedError, match="may or may not have run"):
        await in_flight
    with pytest.raises(WorkspaceDisconnectedError):
        await session.read_text("x")
    assert await session.background_processes() == []
    assert session.closed  # what a holder across runs checks before reuse
    await session.close()  # no error: it is gone already
    # (Killed outright, the far side could not clean up after itself — the
    # two commands are this test's to reap.)
    for pid in (started.pid, int((tmp_path / "pid").read_text())):
        os.killpg(pid, signal.SIGKILL)  # each leads its own group

    # The host reconnects for the next session.
    again = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    assert (await again.run("echo back")).stdout == "back\n"


async def test_closing_the_host_ends_the_far_side_and_its_processes(tmp_path) -> None:
    host = _host()
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    started = await session.spawn("sleep 30")
    far_pid = host._conn.ready["pid"]
    await host.aclose()
    assert await _dead(started.pid) and await _dead(far_pid)


async def test_a_far_side_that_cannot_start_says_why(tmp_path) -> None:
    host = RemoteHost("nowhere", command=(), python="false")
    with pytest.raises(WorkspaceError, match="Could not start") as caught:
        await host.open(str(tmp_path), policy=WorkspacePolicy.coding())
    assert "python3 >= 3.10" in str(caught.value)


async def test_a_silent_far_side_is_given_up_on(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(host_module, "_PING_SECONDS", 0.05)
    monkeypatch.setattr(host_module, "_PING_TIMEOUT", 0.2)
    host = _host()
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    far_pid = host._conn.ready["pid"]
    os.kill(far_pid, signal.SIGSTOP)  # alive, but answering nothing
    try:
        with pytest.raises(WorkspaceDisconnectedError, match="stopped answering"):
            await session.run("echo never")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(far_pid, signal.SIGKILL)
        await host.aclose()


async def test_a_far_side_that_stops_reading_a_large_request_is_given_up_on(
    tmp_path, monkeypatch
) -> None:
    # The request is bigger than the pipe holds, so it sits half-sent — and
    # the pings queueing up behind it must not pass for progress.
    monkeypatch.setattr(host_module, "_PING_SECONDS", 0.05)
    monkeypatch.setattr(host_module, "_PING_TIMEOUT", 0.2)
    host = _host()
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    far_pid = host._conn.ready["pid"]
    os.kill(far_pid, signal.SIGSTOP)
    try:
        with pytest.raises(WorkspaceDisconnectedError, match="stopped answering"):
            await asyncio.wait_for(
                session.write_bytes("big.bin", os.urandom(4_000_000)), timeout=10
            )
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(far_pid, signal.SIGKILL)
        await host.aclose()


async def test_progress_is_bytes_that_left_not_bytes_that_queued(tmp_path) -> None:
    # What the idle check asks while a request is on its way out. (The real
    # timings are untouched here, so nothing else calls it meanwhile.)
    host = _host()
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    conn, far_pid = host._conn, host._conn.ready["pid"]
    blob = os.urandom(4_000_000)
    try:
        os.kill(far_pid, signal.SIGSTOP)
        writing = asyncio.create_task(session.write_bytes("big.bin", blob))
        await asyncio.sleep(0.2)  # the pipe takes what it can hold, then no more
        assert conn._sending() is True  # some of it left since the last look
        assert conn._sending() is False  # and nothing since
        conn._write({"id": 0, "method": "ping"})
        assert conn._sending() is False  # a ping joining the queue is no progress

        os.kill(far_pid, signal.SIGCONT)  # the far side reads again
        assert (await writing).bytes_written == len(blob)
        assert conn._sending() is False  # all sent: nothing is on its way out
        assert (tmp_path / "big.bin").read_bytes() == blob
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(far_pid, signal.SIGCONT)
        await host.aclose()


async def test_alive_means_bytes_are_moving_not_whole_messages() -> None:
    # A file crawling over a slow link is one very long line. Neither side
    # may take the wait for its end as silence.
    from lovia.workspace.remote.wire import read_lines

    reader = asyncio.StreamReader()

    async def dribble() -> None:
        for piece in [b"x"] * 40 + [b"\n", b"second\n"]:
            await asyncio.sleep(0.02)  # each gap far under the idle bound …
            reader.feed_data(piece)

    feeder = asyncio.create_task(dribble())
    lines = read_lines(reader, idle=0.5)  # … their sum (0.8 s) well over it
    assert [await anext(lines), await anext(lines)] == [b"x" * 40, b"second"]
    await feeder

    with pytest.raises(asyncio.TimeoutError):  # real silence
        await anext(lines)

    # Silence on the way in is no verdict while a request is on its way out.
    excuses = iter([True, True, False])
    waiting = read_lines(reader, idle=0.05, still_alive=lambda: next(excuses))
    started = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.TimeoutError):
        await anext(waiting)
    assert asyncio.get_running_loop().time() - started >= 0.14


async def test_cancelling_replies_in_flight_never_garbles_the_stream(
    host, tmp_path
) -> None:
    # A cancel that lands while a large reply is being written must not let
    # the next message be written into the middle of it.
    (tmp_path / "big.bin").write_bytes(os.urandom(400_000))
    session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
    for delay in (0.0, 0.001, 0.003, 0.006, 0.01):
        tasks = [asyncio.create_task(session.read_bytes("big.bin")) for _ in range(12)]
        await asyncio.sleep(delay)
        for task in tasks[::2]:
            task.cancel()
        done = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(
            isinstance(d, asyncio.CancelledError) or len(d.data) == 400_000
            for d in done
        )
    assert host._conn.lost is None
    assert (await session.run("echo intact")).stdout == "intact\n"


async def test_the_far_working_directory_cannot_shadow_the_stdlib(tmp_path) -> None:
    # A container's WORKDIR is the project; its json.py must not be what the
    # far side imports.
    (tmp_path / "json.py").write_text("raise SystemExit('shadowed')", "utf-8")
    (tmp_path / "platform.py").write_text("raise SystemExit('shadowed')", "utf-8")
    host = RemoteHost(
        "far",
        command=("sh", "-c", f'cd {tmp_path} && exec "$@"', "sh"),
        python=sys.executable,
    )
    try:
        session = await host.open(str(tmp_path), policy=WorkspacePolicy.trusted())
        assert host._conn.ready["cwd"] == str(tmp_path.resolve())
        assert (await session.run("echo fine")).stdout == "fine\n"
    finally:
        await host.aclose()


async def test_a_connect_that_is_cancelled_leaves_nothing_behind(tmp_path) -> None:
    # A transport that never gets as far as ready, with a child of its own
    # (as a wrapper shell or a proxy command would be).
    script = "echo $$ > sh.pid; sleep 30 & echo $! > child.pid; wait"
    host = RemoteHost(
        "slow",
        command=("sh", "-c", f"cd {tmp_path} && {script}", "sh"),
        python=sys.executable,
    )
    opening = asyncio.create_task(
        host.open(str(tmp_path), policy=WorkspacePolicy.coding())
    )
    while (
        not (tmp_path / "child.pid").exists()
        or not (tmp_path / "child.pid").read_text().strip()
    ):
        await asyncio.sleep(0.02)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    for name in ("sh.pid", "child.pid"):  # the transport, and what it started
        assert await _dead(int((tmp_path / name).read_text()))


# ---------------------------------------------------------------------------
# Hosts and the config
# ---------------------------------------------------------------------------


def test_host_commands_are_built_safely() -> None:
    ssh = RemoteHost.ssh("neo@box", options=("-p", "2222"))
    argv = ssh._argv()
    assert argv[:7] == ["ssh", "-T", "-o", "BatchMode=yes", "-p", "2222", "--"]
    # One shell-quoted word for the far shell, after the destination.
    assert argv[7] == "neo@box" and argv[8].startswith("python3 -I -u -c '")
    assert len(argv) == 9

    docker = RemoteHost.docker("api", user="dev", python="/opt/py/bin/python3")
    assert docker._argv()[:8] == [
        "docker", "exec", "-i", "-u", "dev", "api", "/opt/py/bin/python3", "-I"
    ]  # fmt: skip
    assert (ssh.name, docker.name) == ("neo@box", "api")

    for bad in ("-oProxyCommand=evil", "two words", ""):
        with pytest.raises(UserError):
            RemoteHost.ssh(bad)
        with pytest.raises(UserError):
            RemoteHost.docker(bad)


async def test_remote_workspace_runs_an_agent(host, tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("remember the milk", encoding="utf-8")
    workspace = Workspace.remote(host, str(tmp_path), mode="trusted")
    assert isinstance(workspace, RemoteWorkspace)
    # Before any session: a plain directory, by its name.
    assert f"named {tmp_path.name!r}" in workspace.instructions()
    assert "git repository" not in workspace.instructions()

    provider = ScriptedProvider(
        [
            call("read_file", {"path": "notes.txt"}, call_id="r"),
            call("shell", {"command": "echo hi > out.txt"}, call_id="s"),
            text("done"),
        ]
    )
    agent = Agent(name="far", model=provider, workspace=workspace)
    result = await Runner.run(agent, "go")
    outputs = [m.content for m in result.messages if m.role == "tool"]
    assert "remember the milk" in outputs[0]
    assert (tmp_path / "out.txt").read_text() == "hi\n"

    # The prompt the run saw describes the root as the far side found it.
    system = provider.calls[0][0]
    assert "git repository" in str(system) and "uv-managed" in str(system)
    # Same tool bundle as the local workspace under the same policy.
    local = Workspace.local(str(tmp_path), mode="trusted")
    assert [t.name for t in workspace.tools()] == [t.name for t in local.tools()]


async def test_remote_denials_and_asks_reach_the_run(host, tmp_path) -> None:
    workspace = Workspace.remote(host, str(tmp_path), mode="coding")
    provider = ScriptedProvider(
        [
            call("write_file", {"path": "/tmp/evil.txt", "content": "x"}, call_id="w"),
            call("shell", {"command": "git push --force"}, call_id="s"),
            text("done"),
        ]
    )
    result = await Runner.run(
        Agent(name="far", model=provider, workspace=workspace), "go"
    )
    outputs = [m.content for m in result.messages if m.role == "tool"]
    assert "denied by policy" in outputs[0] and "outside the workspace" in outputs[0]
    assert "denied by policy" in outputs[1]
