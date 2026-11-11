"""A remote host: one connection, any number of workspace sessions on it.

:class:`RemoteHost` names a place the engine can be started — an SSH
destination, a running container, anything a command can reach with stdio
piped — and owns the single connection to it. Sessions are cheap once it is
up; the connection is opened on first use and reopened after it is lost.

What "lost" means is fixed here, and it is deliberately blunt. Requests in
flight fail with :class:`WorkspaceDisconnectedError` — outcome unknown, never
retried — and every session on the connection is dead. The far side, for its
part, closes its sessions (killing what they started) the moment its input
ends. A later ``open`` starts over on a fresh connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...exceptions import UserError
from ..errors import WorkspaceDisconnectedError, WorkspaceError
from ..policy import WorkspacePolicy
from ..types import RootFacts, WorkspaceLimits
from .bundle import BOOT, bundle
from .session import RemoteWorkspaceSession
from .wire import PROTOCOL, decode, decode_error, encode

__all__ = ["RemoteHost"]

logger = logging.getLogger(__name__)

# The far side gives up after ``serve.IDLE_SECONDS`` of silence; ping well
# inside that, and give up on it ourselves when a ping goes unanswered.
_PING_SECONDS = 30.0
_PING_TIMEOUT = 20.0
_READY_SECONDS = 30.0
_EXIT_SECONDS = 5.0
_MAX_LINE = 64 * 1024 * 1024


class _Connection:
    """The pipe to one far-side process, and the requests riding on it."""

    def __init__(self, name: str, proc: asyncio.subprocess.Process) -> None:
        self.name = name
        self.ready: dict[str, Any] = {}
        self.lost: str | None = None  # why, once it is
        self._proc = proc
        self._next = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._stderr: deque[str] = deque(maxlen=20)
        self._tasks: list[asyncio.Task[None]] = []
        self.loop = asyncio.get_running_loop()

    @classmethod
    async def open(cls, name: str, argv: Sequence[str]) -> "_Connection":
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_MAX_LINE,
                # Its own group: a Ctrl+C at our terminal must reach the far
                # side as a closed pipe (sessions cleaned up), not as a signal
                # to the transport first.
                start_new_session=True,
            )
        except OSError as exc:
            raise WorkspaceError(
                f"Could not start the transport to {name}: {exc}",
                hint=f"The command was: {shlex.join(argv[:3])} …",
            ) from exc
        self = cls(name, proc)
        self._tasks.append(asyncio.create_task(self._drain_stderr()))
        assert proc.stdin is not None
        proc.stdin.write(bundle())
        try:
            self.ready = await asyncio.wait_for(self._await_ready(), _READY_SECONDS)
        except (asyncio.TimeoutError, WorkspaceError) as exc:
            await self.aclose()
            said = "".join(self._stderr).strip()
            raise WorkspaceError(
                f"Could not start the remote workspace on {name}."
                + (f" It said: {said}" if said else ""),
                hint=(
                    "The far side needs python3 >= 3.10 on its PATH, reachable "
                    "without a prompt (SSH keys or an agent; a running "
                    "container)."
                ),
            ) from exc
        self._tasks.append(asyncio.create_task(self._read()))
        self._tasks.append(asyncio.create_task(self._keep_alive()))
        return self

    async def _await_ready(self) -> dict[str, Any]:
        """The far side's first message — past whatever a login shell's
        startup files chose to print before it."""
        assert self._proc.stdout is not None
        while line := await self._proc.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and "ready" in message:
                if message["ready"] != PROTOCOL:
                    raise WorkspaceError(f"protocol {message['ready']} != {PROTOCOL}")
                return message
        raise WorkspaceError("the far side exited before it was ready")

    async def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        async for raw in self._proc.stderr:
            self._stderr.append(raw.decode("utf-8", "replace"))

    async def _read(self) -> None:
        assert self._proc.stdout is not None
        try:
            while line := await self._proc.stdout.readline():
                message = json.loads(line)
                future = self._pending.pop(message.get("id"), None)
                if future is None or future.done():
                    continue  # the caller gave up on it (cancelled)
                if "error" in message:
                    future.set_exception(decode_error(message["error"]))
                else:
                    future.set_result(decode(message.get("result")))
            self._lose("the connection closed")
        except Exception as exc:  # noqa: BLE001 — a broken stream is a lost connection
            self._lose(f"{type(exc).__name__}: {exc}")

    async def _keep_alive(self) -> None:
        while self.lost is None:
            await asyncio.sleep(_PING_SECONDS)
            try:
                await asyncio.wait_for(self.call("ping"), _PING_TIMEOUT)
            except asyncio.TimeoutError:
                self._lose("it stopped answering")
            except WorkspaceDisconnectedError:
                return

    def _lose(self, why: str) -> None:
        if self.lost is not None:
            return
        self.lost = why
        error = self._disconnected()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        # Our end must go too: the far side cleans up on end of input, and a
        # transport that died does not close the pipe *we* hold.
        self._hang_up()

    def _hang_up(self) -> None:
        with contextlib.suppress(Exception):
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        with contextlib.suppress(ProcessLookupError):
            if self._proc.returncode is None:
                self._proc.kill()

    def _disconnected(self) -> WorkspaceDisconnectedError:
        said = "".join(self._stderr).strip()
        return WorkspaceDisconnectedError(
            f"Lost the connection to {self.name} ({self.lost}). An operation "
            "in flight may or may not have run.",
            hint=said or "Check on the far side before repeating anything.",
        )

    def _write(self, message: dict[str, Any]) -> None:
        if self.lost is not None:
            raise self._disconnected()
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))

    async def call(
        self, method: str, sid: str | None = None, params: Any = None
    ) -> Any:
        self._next += 1
        request_id = self._next
        future: asyncio.Future[Any] = self.loop.create_future()
        self._write(
            {"id": request_id, "method": method, "sid": sid, "params": encode(params)}
        )
        self._pending[request_id] = future
        try:
            return await future
        except asyncio.CancelledError:
            # Whoever awaited this gave up (a stopped run, a timeout): tell
            # the far side, which kills what the request had started.
            self._pending.pop(request_id, None)
            if self.lost is None:
                with contextlib.suppress(Exception):
                    self.notify("cancel", params={"id": request_id})
            raise

    def notify(self, method: str, sid: str | None = None, params: Any = None) -> None:
        self._write({"method": method, "sid": sid, "params": encode(params)})

    async def aclose(self) -> None:
        if self.lost is None:
            self.lost = "closed"
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._disconnected())
        self._pending.clear()
        with contextlib.suppress(Exception):
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        # Bounded: asyncio resolves wait() only once every pipe has closed,
        # which a far-side straggler holding stdout could postpone forever.
        try:
            await asyncio.wait_for(self._proc.wait(), _EXIT_SECONDS)
        except asyncio.TimeoutError:
            self._hang_up()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._proc.wait(), _EXIT_SECONDS)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def _checked(value: str, what: str) -> str:
    """``value`` as one safe command-line word: never an option in disguise."""
    if not value or value.startswith("-") or any(c.isspace() for c in value):
        raise UserError(
            f"Invalid {what}: {value!r}",
            hint="It must be one word and must not start with '-'.",
        )
    return value


@dataclass(eq=False)
class RemoteHost:
    """Somewhere a workspace can live other than this machine.

    ``command`` runs its trailing arguments on the far side with stdio piped
    — ``("ssh", "box")``, ``("docker", "exec", "-i", "api")``, ``("kubectl",
    "exec", "-i", "pod", "--")``. Set ``quote`` when it hands them to a shell
    there (ssh does). ``python`` is the far interpreter, 3.10 or newer; it
    may carry arguments (``"/opt/py/bin/python3 -I"``).

    ``name`` identifies the host to the people and the records that refer to
    it (an approval remembered for ``box`` is not one for ``api``).

    Build the two common kinds with :meth:`ssh` and :meth:`docker`, hand the
    host to :meth:`Workspace.remote <lovia.workspace.Workspace.remote>`, and
    :meth:`aclose` it when done::

        box = RemoteHost.ssh("box")
        agent = Agent(..., workspace=Workspace.remote(box, "/srv/project"))
    """

    name: str
    command: Sequence[str]
    python: str = "python3"
    quote: bool = False
    _conn: _Connection | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _facts: dict[str, RootFacts] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def ssh(
        cls,
        destination: str,
        *,
        name: str | None = None,
        python: str = "python3",
        options: Sequence[str] = (),
    ) -> "RemoteHost":
        """An SSH destination: a ``Host`` from ``~/.ssh/config`` or
        ``user@host``. Everything about reaching it (port, key, jump host)
        belongs in the SSH config; ``options`` passes extra ``ssh`` arguments.

        Always non-interactive (``BatchMode``): the pipe carries the protocol,
        so a password or host-key prompt could only hang. Set up key or agent
        authentication first.
        """
        return cls(
            name=name or destination,
            command=(
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                *options,
                "--",
                _checked(destination, "SSH destination"),
            ),
            python=python,
            quote=True,
        )

    @classmethod
    def docker(
        cls,
        container: str,
        *,
        user: str | None = None,
        name: str | None = None,
        python: str = "python3",
    ) -> "RemoteHost":
        """A running container on this machine's Docker. ``user`` is who to
        be inside it — the default is the image's, often root, and files the
        agent writes are owned accordingly."""
        as_user = ("-u", _checked(user, "container user")) if user else ()
        return cls(
            name=name or container,
            command=(
                "docker",
                "exec",
                "-i",
                *as_user,
                _checked(container, "container"),
            ),
            python=python,
        )

    def _argv(self) -> list[str]:
        far = [*shlex.split(self.python), "-u", "-c", BOOT]
        return [*self.command, shlex.join(far)] if self.quote else [*self.command, *far]

    async def _connection(self) -> _Connection:
        async with self._lock:
            conn = self._conn
            if conn is not None and conn.loop is not asyncio.get_running_loop():
                # Opened under an event loop that is gone (one asyncio.run
                # per call): nothing of it can be awaited any more.
                conn._hang_up()
                conn = None
            if conn is None or conn.lost is not None:
                if conn is not None:
                    await conn.aclose()
                conn = self._conn = await _Connection.open(self.name, self._argv())
            return conn

    async def open(
        self,
        root: str,
        *,
        policy: WorkspacePolicy,
        env: Mapping[str, str] | None = None,
        shell_timeout: float | None = 300.0,
        limits: WorkspaceLimits | None = None,
        inherit_env: bool = False,
    ) -> RemoteWorkspaceSession:
        """Open a session rooted at ``root`` on this host, connecting first if
        need be. :class:`RemoteWorkspace` is the usual way here."""
        conn = await self._connection()
        opened = await conn.call(
            "open",
            params={
                "root": root,
                "policy": policy,
                "env": dict(env) if env is not None else None,
                "shell_timeout": shell_timeout,
                "limits": limits if limits is not None else WorkspaceLimits(),
                "inherit_env": inherit_env,
            },
        )
        self._facts[root] = opened["facts"]
        return RemoteWorkspaceSession(conn, opened["sid"], opened["root"], policy)

    def facts(self, root: str) -> RootFacts | None:
        """What the last session opened at ``root`` reported about it."""
        return self._facts.get(root)

    async def aclose(self) -> None:
        """Close the connection; the far side closes every session with it."""
        async with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None and conn.loop is asyncio.get_running_loop():
            await conn.aclose()
        elif conn is not None:
            conn._hang_up()
