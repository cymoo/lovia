"""The far side of a remote workspace: real sessions behind a pipe.

Started by the bootstrap (:mod:`.bundle`) from source it was sent, this loop
owns the :class:`~lovia.workspace.local.LocalWorkspaceSession` objects and
answers the near side's requests (:mod:`.wire`). Policy is judged here, next
to the files — it has to be: listing and search consult it per entry.

Lifetime is the pipe's. End of input, or silence longer than
:data:`IDLE_SECONDS` (a connection that died without closing), closes every
session — which kills what they started — and exits.

Part of the stdlib-only engine: this runs where lovia is not installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
from functools import partial
from pathlib import Path
from typing import Any

from ..errors import WorkspaceClosedError
from ..local import LocalWorkspaceSession, root_facts
from .wire import PROTOCOL, decode, encode, encode_error, read_lines

__all__ = ["main"]

# Without a byte from the near side for this long, it is gone. It pings well
# inside this; see ``host._PING_SECONDS``.
IDLE_SECONDS = 120.0

# What a request may call on a session — the WorkspaceSession protocol.
_SESSION_METHODS = frozenset(
    {
        "background_processes",
        "decide_command",
        "decide_path",
        "edit_text",
        "grep",
        "kill_process",
        "list_files",
        "read_bytes",
        "read_process_output",
        "read_text",
        "run",
        "spawn",
        "write_bytes",
        "write_text",
    }
)


def _take_stdio() -> tuple[int, int]:
    """Move the protocol off fds 0 and 1, so nothing a command inherits and
    nothing that prints can reach it: 0 becomes /dev/null, 1 becomes stderr."""
    rpc_in, rpc_out = os.dup(0), os.dup(1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    os.set_inheritable(rpc_in, False)
    os.set_inheritable(rpc_out, False)
    return rpc_in, rpc_out


class _Server:
    def __init__(self, rpc_in: int, rpc_out: int) -> None:
        self._in, self._out = rpc_in, rpc_out
        self._sessions: dict[str, LocalWorkspaceSession] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._closing: set[asyncio.Future[None]] = set()  # kept from the GC
        self._write_lock = asyncio.Lock()

    async def _send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        # Shielded: a request cancelled while its reply is going out must not
        # release the lock under the writing thread — the next message would
        # be written into the middle of this one.
        await asyncio.shield(self._send_whole(data))

    async def _send_whole(self, data: bytes) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._write_all, data)

    def _write_all(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self._out, view) :]

    async def serve(self) -> None:
        reader = asyncio.StreamReader()
        await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(self._in, "rb", 0)
        )
        await self._send(
            {
                "ready": PROTOCOL,
                "python": platform.python_version(),
                "platform": sys.platform,
                "pid": os.getpid(),
                "cwd": os.getcwd(),
                "home": os.path.expanduser("~"),
            }
        )
        try:
            async for line in read_lines(reader, IDLE_SECONDS):
                message = json.loads(line)
                if "id" not in message:
                    self._notified(message)
                    continue
                request_id: int = message["id"]
                task = asyncio.create_task(self._answer(message))
                self._tasks[request_id] = task
                task.add_done_callback(partial(self._forget, request_id))
        except asyncio.TimeoutError:
            pass  # the near side is gone without saying so
        finally:
            for task in list(self._tasks.values()):
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            for session in list(self._sessions.values()):
                await session.close()

    def _forget(self, request_id: int, _task: "asyncio.Task[None]") -> None:
        self._tasks.pop(request_id, None)

    def _notified(self, message: dict[str, Any]) -> None:
        """Handled inline, in arrival order — so a policy change is in force
        for every request that was sent after it."""
        method = message.get("method")
        session = self._sessions.get(message.get("sid") or "")
        try:
            params = decode(message.get("params")) or {}
            if method == "cancel":
                task = self._tasks.get(int(params["id"]))
                if task is not None:
                    task.cancel()
            elif method == "set_policy" and session is not None:
                session.set_policy(params["policy"])
        except Exception as exc:  # noqa: BLE001 — one bad message must not end them all
            print(f"lovia: bad {method} notification: {exc!r}", file=sys.stderr)
            if method == "set_policy" and session is not None:
                # The near side now believes in a policy this session does
                # not have. Better no session than one judging by the old.
                del self._sessions[session.id]
                self._closing.add(asyncio.ensure_future(session.close()))

    async def _answer(self, message: dict[str, Any]) -> None:
        try:
            result = await self._call(
                message["method"], message.get("sid"), decode(message.get("params"))
            )
            reply: dict[str, Any] = {"id": message["id"], "result": encode(result)}
        except asyncio.CancelledError:
            # The reply to a cancelled request still goes out (_send shields).
            cancelled = {"type": "Cancelled", "message": "cancelled", "hint": None}
            await self._send({"id": message["id"], "error": cancelled})
            raise
        except Exception as exc:  # noqa: BLE001 — every failure goes back over the wire
            reply = {"id": message["id"], "error": encode_error(exc)}
        await self._send(reply)

    async def _call(self, method: str, sid: str | None, params: Any) -> Any:
        params = params or {}
        if method == "ping":
            return None
        if method == "open":
            opened = LocalWorkspaceSession(**params)
            self._sessions[opened.id] = opened
            root = Path(opened.root).expanduser().resolve()
            return {
                "sid": opened.id,
                "root": root.as_posix(),
                "facts": root_facts(root),
            }
        session = self._sessions.get(sid or "")
        if session is None:
            raise WorkspaceClosedError(f"Workspace session {sid} is closed.")
        if method == "close":
            del self._sessions[session.id]
            await session.close()
            return None
        if method not in _SESSION_METHODS:
            raise ValueError(f"unknown method {method!r}")
        result = getattr(session, method)(**params)
        return await result if asyncio.iscoroutine(result) else result


def main() -> None:
    asyncio.run(_Server(*_take_stdio()).serve())
