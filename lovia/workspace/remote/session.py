"""The near side of a remote session: every call is a request to the far one.

Implements :class:`~lovia.workspace.protocol.WorkspaceSession` by forwarding.
Nothing is judged or cached here — the far session is the single enforcement
point, exactly as the local one is locally — so the built-in tools and the
runner cannot tell the difference, apart from the wait.
"""

# Every method returns what the far side sent back, decoded by the wire into
# the type its signature names; the static checker only sees the ``Any``.
# mypy: disable-error-code="no-any-return"

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ...approvals import Verdict
from ..errors import WorkspaceClosedError, WorkspaceDisconnectedError
from ..policy import WorkspacePolicy
from ..types import (
    CommandResult,
    DirEntry,
    EditResult,
    FileBytes,
    FileChange,
    FileContent,
    GrepMatch,
    ProcessOutput,
    ProcessStart,
    ProcessStatus,
)
from .wire import encode

if TYPE_CHECKING:
    from .host import _Connection

__all__ = ["RemoteWorkspaceSession"]

# A verdict that takes longer than this is no verdict: the tools then ask.
_DECIDE_SECONDS = 15.0


class RemoteWorkspaceSession:
    """A workspace session that lives on a :class:`~.host.RemoteHost`."""

    def __init__(
        self, conn: "_Connection", sid: str, root: str, policy: WorkspacePolicy
    ) -> None:
        self.id = sid
        self.root = root
        """The root as the far side resolved it."""
        self.policy = policy
        self._conn = conn
        self._closed = False

    async def _call(self, method: str, **params: Any) -> Any:
        if self._closed:
            raise WorkspaceClosedError(f"Workspace session {self.id} is closed.")
        return await self._conn.call(method, self.id, params)

    def set_policy(self, policy: WorkspacePolicy) -> None:
        # Encoded first: a policy that cannot travel must fail before the
        # two sides can disagree. Sent as a notification, which the far side
        # applies in order — ahead of any request made after this returns.
        encode(policy)
        self._conn.notify("set_policy", self.id, {"policy": policy})
        self.policy = policy

    async def decide_path(self, path: str, *, write: bool = False) -> Verdict:
        return await asyncio.wait_for(
            self._call("decide_path", path=path, write=write), _DECIDE_SECONDS
        )

    async def decide_command(self, command: str, cwd: str = ".") -> Verdict:
        return await asyncio.wait_for(
            self._call("decide_command", command=command, cwd=cwd), _DECIDE_SECONDS
        )

    async def read_text(
        self, path: str, *, start: int | None = None, end: int | None = None
    ) -> FileContent:
        return await self._call("read_text", path=path, start=start, end=end)

    async def read_bytes(self, path: str, *, max_bytes: int | None = None) -> FileBytes:
        return await self._call("read_bytes", path=path, max_bytes=max_bytes)

    async def write_text(
        self, path: str, content: str, *, create_only: bool = False
    ) -> FileChange:
        return await self._call(
            "write_text", path=path, content=content, create_only=create_only
        )

    async def write_bytes(
        self, path: str, data: bytes, *, create_only: bool = False
    ) -> FileChange:
        return await self._call(
            "write_bytes", path=path, data=data, create_only=create_only
        )

    async def edit_text(
        self, path: str, old: str, new: str, *, replace_all: bool = False
    ) -> EditResult:
        return await self._call(
            "edit_text", path=path, old=old, new=new, replace_all=replace_all
        )

    async def list_files(
        self,
        path: str = ".",
        *,
        pattern: str | None = None,
        include_hidden: bool = False,
        max_results: int | None = None,
    ) -> list[DirEntry]:
        return await self._call(
            "list_files",
            path=path,
            pattern=pattern,
            include_hidden=include_hidden,
            max_results=max_results,
        )

    async def grep(
        self,
        pattern: str,
        *,
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = False,
        include_hidden: bool = False,
        max_matches: int | None = None,
    ) -> list[GrepMatch]:
        return await self._call(
            "grep",
            pattern=pattern,
            path=path,
            glob=glob,
            ignore_case=ignore_case,
            include_hidden=include_hidden,
            max_matches=max_matches,
        )

    async def run(
        self,
        command: str,
        *,
        cwd: str = ".",
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        return await self._call(
            "run",
            command=command,
            cwd=cwd,
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )

    async def spawn(
        self,
        command: str,
        *,
        cwd: str = ".",
        env: Mapping[str, str] | None = None,
    ) -> ProcessStart:
        return await self._call(
            "spawn",
            command=command,
            cwd=cwd,
            env=dict(env) if env is not None else None,
        )

    async def read_process_output(self, process_id: str) -> ProcessOutput:
        return await self._call("read_process_output", process_id=process_id)

    async def kill_process(self, process_id: str) -> ProcessOutput:
        return await self._call("kill_process", process_id=process_id)

    async def background_processes(self) -> list[ProcessStatus]:
        # Passive, like the local one: nothing to list is not an error, and
        # neither is a session that is gone (its processes went with it).
        if self._closed:
            return []
        try:
            return await self._call("background_processes")
        except (WorkspaceClosedError, WorkspaceDisconnectedError):
            return []

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self._call("close")
        except (WorkspaceClosedError, WorkspaceDisconnectedError):
            pass  # already gone, and its processes with it
        finally:
            self._closed = True
