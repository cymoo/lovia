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
import contextlib
import errno
import json
import os
import platform
import re
import stat
import sys
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Any

from ...exceptions import UserError
from ..errors import WorkspaceClosedError
from ..local import LocalWorkspaceSession, root_facts
from ..paths import complete_dirs
from .wire import PROTOCOL, decode, encode, encode_error, frame, read_lines, unframe

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
        "peek_process_output",
        "read_bytes",
        "read_instructions",
        "read_process_output",
        "read_text",
        "run",
        "spawn",
        "stat",
        "stop_process",
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
        data = frame(json.dumps(message, ensure_ascii=False).encode("utf-8"))
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
                message = unframe(line)
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
        if method == "resolve_root":
            return _resolve_root(params["path"])
        if method == "complete_dirs":
            return await asyncio.to_thread(complete_dirs, params["path"])
        if method == "attachments_list":
            return await asyncio.to_thread(attachments_list, params["id"])
        if method == "attachments_put":
            return await asyncio.to_thread(
                attachments_put,
                params["id"],
                params["name"],
                params["data"],
                params["mtime"],
            )
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


def _resolve_root(path: str) -> str:
    """``path`` as a workspace root here: absolute (or ``~``), resolved, and
    an existing directory."""
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        raise UserError(
            f"{path!r} is not an absolute path",
            hint="give the full path, e.g. /srv/project or ~/project",
        )
    resolved = expanded.resolve()
    if not resolved.is_dir():
        raise UserError(f"No such directory: {resolved}")
    return resolved.as_posix()


# A chat's attachment folder: the hex id the near side names it by.
_ATTACHMENTS_ID = re.compile(r"[0-9a-f]{32}")


def _attachments_base() -> Path:
    """The account's own folder under this host's temp directory — named
    only: :func:`_attachments_root` makes it and vouches for it."""
    tmp = Path(tempfile.gettempdir()).resolve()
    return tmp / ("lovia" if os.name != "posix" else f"lovia-{os.getuid()}")


def _attachments_root() -> Path:
    """Where this host keeps copies of chats' attachments: under its temp
    directory, in a folder this account alone owns. The copies are a cache —
    the near side holds the originals and puts back what is missing — so the
    system's temp cleaning is all the deleting they get.

    POSIX ``/tmp`` is shared: the account's folder is made ``0700`` and, if
    it was already there, must be a real directory this account owns and no
    one else may enter — or another account that made it first (or planted a
    link) would be handed the files. Windows' ``%TEMP%`` is the account's own.
    """
    base = _attachments_base()
    if os.name != "posix":
        base.mkdir(exist_ok=True)
    else:
        try:
            base.mkdir(mode=0o700)
        except FileExistsError:
            pass
        st = os.lstat(base)
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
        ):
            raise PermissionError(
                errno.EACCES,
                "not a private directory of this account; attachments are "
                "not copied into it",
                str(base),
            )
    root = base / "attachments"
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def _attachments_folder(chat: str) -> Path:
    if not _ATTACHMENTS_ID.fullmatch(chat):
        raise ValueError(f"not an attachment folder id: {chat!r}")
    return _attachments_root() / chat


# A put in progress writes here first. No attachment name starts with a dot.
_PUTTING = ".put-"
# A put's temp file older than this was left by one that never finished.
_ABANDONED = 3600.0


def attachments_list(chat: str) -> dict[str, Any]:
    """The folder ``chat``'s attachments are copied to here, as a POSIX path
    (Windows takes ``/`` too), and each copy already in it by name, with its
    size and whole-second mtime — a put stamps the original's, so a copy
    changed here no longer matches. A put's temp file is no copy: skipped,
    and removed once it is old enough to be one a dropped host left.

    A folder this host won't vouch for (:func:`_attachments_root`) is still
    named, with why as ``refused``: whatever copies sit there must stay
    read-only to the chat's runs, and the near side needs the path for that.
    """
    if not _ATTACHMENTS_ID.fullmatch(chat):
        raise ValueError(f"not an attachment folder id: {chat!r}")
    named = _attachments_base() / "attachments" / chat
    try:
        folder = _attachments_root() / chat
    except PermissionError as exc:
        return {"folder": _real(named), "files": {}, "refused": str(exc)}
    files: dict[str, list[int]] = {}
    if folder.is_dir():
        now = time.time()
        for entry in os.scandir(folder):
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:  # a put renamed it into place meanwhile
                continue
            if not entry.name.startswith(_PUTTING):
                files[entry.name] = [st.st_size, int(st.st_mtime)]
            elif now - st.st_mtime > _ABANDONED:
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)
    return {"folder": _real(folder), "files": files, "refused": None}


def _real(folder: Path) -> str:
    """``folder`` as policy sees it: links resolved, since a path is judged
    by where it really leads — a rule naming the link would miss."""
    return folder.resolve().as_posix()


def attachments_put(chat: str, name: str, data: bytes, mtime: float) -> None:
    """Write one copy into ``chat``'s folder here, replacing a stale one
    whole (a temp file renamed over it — a reader never sees half), stamped
    with the original's ``mtime``."""
    if name in ("", ".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise ValueError(f"not a file name: {name!r}")
    folder = _attachments_folder(chat)
    folder.mkdir(mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=folder, prefix=_PUTTING)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(temp, folder / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    # Stamped once in place: a temp file dated back to the original's mtime
    # would look abandoned to a listing that runs meanwhile, which removes it.
    os.utime(folder / name, (mtime, mtime))


def main() -> None:
    asyncio.run(_Server(*_take_stdio()).serve())
