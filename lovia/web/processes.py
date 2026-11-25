"""Background commands as the web layer sees them: live ones in a chat's
workspace session, finished ones as records (:mod:`.process_logs`).

:class:`ProcessWatcher` looks at the sessions that have something running,
announces what changed (``processes_changed``) and writes the record of a
command once its output has closed. A session that closes — the server goes
down, the chat moves to another directory — is wound up first
(:meth:`ProcessWatcher.closing`), so its commands leave a record too. Only
deleting the chat leaves none.

Nothing here consumes output: the model's own reads are unaffected.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from ..workspace import ProcessChunk, ProcessStatus, WorkspaceError
from ..workspace.protocol import WorkspaceSession
from ..workspace.remote.session import RemoteWorkspaceSession
from .process_logs import (
    EndedBy,
    ProcessRecord,
    load_records,
    read_record,
    save_record,
)
from .schemas import ProcessInfo
from .sse import ProcessesChangedData
from .workspaces import CloseReason

if TYPE_CHECKING:
    from .api.deps import RouterDeps

__all__ = ["OUTPUT_PAGE_CHARS", "ProcessWatcher"]

log = logging.getLogger(__name__)

# Most one page of output carries.
OUTPUT_PAGE_CHARS = 256 * 1024

# How often a session with something running is looked at.
_EVERY = 2.0

# How long a closing session may take to hand over what its commands wrote.
_CLOSE_TIMEOUT = 2.0

# A remote host's clock is taken to agree with ours within this.
_SAME_CLOCK = 5.0


@dataclass(eq=False)
class _Watch:
    """What is known of one workspace session's commands."""

    session: WorkspaceSession
    known: dict[str, ProcessStatus] = field(default_factory=dict)
    # Commands that have their record, or never will.
    settled: set[str] = field(default_factory=set)
    user_stops: set[str] = field(default_factory=set)
    # Added to the session's times to make them this machine's.
    skew: float | None = None
    # Whether to keep looking, and whether a mark came while a look was under
    # way (which may have missed what the mark was for).
    active: bool = False
    marked: bool = False
    closed: bool = False
    # Its chat was deleted: a look under way must leave no file behind.
    discarded: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _Output:
    """A command's output as fetched so far."""

    base: int = 0
    text: list[str] = field(default_factory=list)

    @property
    def end(self) -> int:
        return self.base + sum(len(part) for part in self.text)

    def add(self, chunk: ProcessChunk) -> None:
        start = chunk.next - len(chunk.text)
        if start != self.end:  # what we had is gone from under the buffer
            self.base, self.text = start, []
        if chunk.text:
            self.text.append(chunk.text)


class ProcessWatcher:
    def __init__(self, deps: "RouterDeps") -> None:
        self._deps = deps
        self._watches: dict[str, _Watch] = {}
        self._looks: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()

    # ------------------------------------------------------------ watching -

    async def run(self) -> None:
        """Look at what is running every couple of seconds, until cancelled."""
        try:
            while True:
                self._wake.clear()
                active = [sid for sid, w in self._watches.items() if w.active]
                for sid in active:
                    # A task each: a host that answers slowly holds up its
                    # own chat only.
                    if sid not in self._looks:
                        task = asyncio.create_task(self._look_quietly(sid))
                        self._looks[sid] = task
                        task.add_done_callback(partial(self._looked, sid))
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), _EVERY if active else None
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            looks = list(self._looks.values())
            for task in looks:
                task.cancel()
            await asyncio.gather(*looks, return_exceptions=True)

    def _looked(self, session_id: str, _task: "asyncio.Task[None]") -> None:
        self._looks.pop(session_id, None)

    def mark(self, session_id: str) -> None:
        """Something in the chat's workspace session may have changed."""
        watch = self._watch(session_id)
        if watch is not None:
            watch.active = watch.marked = True
            self._wake.set()

    def running(self, session_id: str) -> int:
        watch = self._watch(session_id, create=False)
        if watch is None:
            return 0
        return sum(s.status == "running" for s in watch.known.values())

    def _watch(self, session_id: str, *, create: bool = True) -> _Watch | None:
        """The watch on the chat's live session, new when the session is."""
        session = self._deps.workspaces.get(session_id)
        watch = self._watches.get(session_id)
        if watch is not None and watch.session is session:
            return watch
        if session is None or not create:
            return None
        watch = self._watches[session_id] = _Watch(session)
        return watch

    async def _look_quietly(self, session_id: str) -> None:
        try:
            await self._look(session_id)
        except Exception as exc:  # noqa: BLE001 — the next look tries again
            log.debug("looking at processes of %s failed: %r", session_id, exc)

    async def _look(self, session_id: str) -> None:
        """Announce what changed and record what finished."""
        watch = self._watch(session_id)
        if watch is None:
            self._watches.pop(session_id, None)
            return
        async with watch.lock:
            watch.marked = False
            statuses = await self._statuses(watch)
            if watch.closed:
                return
            if getattr(watch.session, "closed", False):
                # A remote session that went with its connection.
                await self._wind_up(session_id, watch, "lost")
                return
            self._note(session_id, watch, statuses)
            for status in statuses:
                if (
                    status.status != "running"
                    and status.output_closed
                    and status.process_id not in watch.settled
                ):
                    output = _Output()
                    await self._fetch(watch, status.process_id, output)
                    await self._settle(session_id, watch, status, output)
            watch.active = watch.marked or any(
                s.process_id not in watch.settled for s in statuses
            )

    async def _statuses(self, watch: _Watch) -> list[ProcessStatus]:
        listed = watch.session.background_processes()
        if inspect.isawaitable(listed):  # a remote session asks its host
            listed = await listed
        if not listed:
            return []
        if watch.skew is None:
            skew = 0.0
            if isinstance(watch.session, RemoteWorkspaceSession):
                # Told of it right after it started, so that is about now.
                ahead = time.time() - listed[-1].started_at
                skew = ahead if abs(ahead) > _SAME_CLOCK else 0.0
            watch.skew = skew
        if not watch.skew:
            return listed
        return [
            replace(
                s,
                started_at=s.started_at + watch.skew,
                ended_at=s.ended_at and s.ended_at + watch.skew,
            )
            for s in listed
        ]

    def _note(
        self, session_id: str, watch: _Watch, statuses: list[ProcessStatus]
    ) -> None:
        before = {pid: s.status for pid, s in watch.known.items()}
        watch.known = {s.process_id: s for s in statuses}
        if before != {s.process_id: s.status for s in statuses}:
            self._announce(session_id, watch)

    def _announce(self, session_id: str, watch: _Watch) -> None:
        running = sum(s.status == "running" for s in watch.known.values())
        self._deps.emit(
            "processes_changed",
            ProcessesChangedData(session_id=session_id, running=running),
        )

    # ------------------------------------------------------------- records -

    async def _fetch(self, watch: _Watch, process_id: str, into: _Output) -> None:
        """Collect a command's output until it closes."""
        while True:
            chunk = await watch.session.peek_process_output(
                process_id, after=into.end, limit=OUTPUT_PAGE_CHARS
            )
            into.add(chunk)
            if chunk.eof:
                return
            if not chunk.text:
                await asyncio.sleep(0.05)

    async def _settle(
        self,
        session_id: str,
        watch: _Watch,
        status: ProcessStatus,
        output: _Output,
        ended_by: EndedBy | None = None,
    ) -> None:
        """Write a command's record. ``status`` is how it ended."""
        watch.settled.add(status.process_id)
        folder = self._deps.store.process_log_folder(session_id)
        if folder is None or watch.discarded:
            return
        text = "".join(output.text)
        record = ProcessRecord(
            id=status.process_id,
            command=status.command,
            cwd=status.cwd,
            started_at=status.started_at,
            ended_at=status.ended_at or time.time(),
            status="exited" if status.status == "exited" else "killed",
            exit_code=status.exit_code,
            ended_by=ended_by or _ended_by(watch, status),
            base=output.base,
            chars=len(text),
        )
        try:
            await asyncio.to_thread(save_record, folder, record, text)
        except OSError as exc:
            log.warning("recording process %s failed: %s", record.id, exc)

    async def closing(
        self,
        session_id: str,
        session: WorkspaceSession,
        reason: CloseReason | None,
    ) -> None:
        """Record what a session about to close leaves, stopping what still
        runs. ``reason`` None: the chat is being deleted, keep nothing."""
        watch = self._watches.get(session_id)
        if watch is None or watch.session is not session:
            return
        if reason is None:
            del self._watches[session_id]
            watch.closed = watch.discarded = True
        elif getattr(session, "closed", False):
            await self._wind_up(session_id, watch, "lost")
        else:
            await self._wind_up(session_id, watch, reason)

    async def _wind_up(self, session_id: str, watch: _Watch, reason: EndedBy) -> None:
        if watch.closed:
            return
        watch.closed = True
        if self._watches.get(session_id) is watch:
            del self._watches[session_id]
        statuses = list(watch.known.values())
        if reason != "lost":
            try:
                statuses = await asyncio.wait_for(self._statuses(watch), _CLOSE_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 — record what is known
                log.debug("listing processes of %s failed: %s", session_id, exc)
        left = [s for s in statuses if s.process_id not in watch.settled]
        await asyncio.gather(
            *(self._wind_up_one(session_id, watch, s, reason) for s in left)
        )
        watch.known = {}
        if left:
            self._announce(session_id, watch)

    async def _wind_up_one(
        self, session_id: str, watch: _Watch, status: ProcessStatus, reason: EndedBy
    ) -> None:
        output = _Output()
        if reason != "lost":
            try:
                status_after = await asyncio.wait_for(
                    self._stop_and_fetch(watch, status, output), _CLOSE_TIMEOUT
                )
            except Exception as exc:  # noqa: BLE001 — keep what was fetched
                log.debug("winding up %s: %r", status.process_id, exc)
            else:
                if status.status == "running":
                    status = replace(
                        status_after,
                        started_at=status.started_at,
                        ended_at=time.time(),
                    )
                    await self._settle(session_id, watch, status, output, reason)
                    return
        if status.status == "running":
            status = replace(status, status="killed", ended_at=time.time())
            await self._settle(session_id, watch, status, output, reason)
        else:
            await self._settle(session_id, watch, status, output)

    async def _stop_and_fetch(
        self, watch: _Watch, status: ProcessStatus, output: _Output
    ) -> ProcessStatus:
        # At once: a stop waits for output something else may keep open, and
        # the fetch must not wait with it.
        stop = asyncio.ensure_future(watch.session.stop_process(status.process_id))
        try:
            await self._fetch(watch, status.process_id, output)
            return await stop
        finally:
            stop.cancel()

    # ----------------------------------------------------------------- API -

    async def infos(self, session_id: str) -> list[ProcessInfo]:
        """The chat's commands, oldest first: the live session's, and the
        records of those that are over."""
        out: dict[str, ProcessInfo] = {}
        watch = self._watch(session_id)
        if watch is not None:
            for s in await self._statuses(watch):
                if s.process_id not in watch.known:
                    self.mark(session_id)  # started past the marks
                out[s.process_id] = ProcessInfo(
                    process_id=s.process_id,
                    command=s.command,
                    status=s.status,
                    exit_code=s.exit_code,
                    cwd=s.cwd,
                    started_at=s.started_at,
                    ended_at=s.ended_at,
                    ended_by=None if s.status == "running" else _ended_by(watch, s),
                    output_chars=s.output_chars,
                    last_line=s.last_line,
                )
        folder = self._deps.store.process_log_folder(session_id)
        if folder is not None:
            for r in await asyncio.to_thread(load_records, folder):
                out[r.id] = ProcessInfo(
                    process_id=r.id,
                    command=r.command,
                    status=r.status,
                    exit_code=r.exit_code,
                    cwd=r.cwd,
                    started_at=r.started_at,
                    ended_at=r.ended_at,
                    ended_by=r.ended_by,
                    output_chars=r.base + r.chars,
                )
        return sorted(out.values(), key=lambda p: p.started_at)

    async def page(
        self, session_id: str, process_id: str, *, after: int, limit: int
    ) -> ProcessChunk:
        """One page of a command's output, from its record when it has one.
        Raises :class:`WorkspaceError` for an unknown command."""
        record = await self._record(session_id, process_id)
        if record is None:
            return await self._live(session_id).peek_process_output(
                process_id, after=after, limit=limit
            )
        base, text = record
        end = base + len(text)
        start = min(max(after, base), end)
        page = text[start - base : start - base + limit]
        return ProcessChunk(
            process_id=process_id,
            text=page,
            base=base,
            next=start + len(page),
            eof=start + len(page) == end,
        )

    async def whole_log(self, session_id: str, process_id: str) -> str:
        """All that is kept of a command's output."""
        record = await self._record(session_id, process_id)
        if record is not None:
            return record[1]
        session = self._live(session_id)
        listed = session.background_processes()
        if inspect.isawaitable(listed):
            listed = await listed
        # What is there by now: a busy command may write on for good.
        goal = max(
            (s.output_chars for s in listed if s.process_id == process_id), default=0
        )
        output = _Output()
        while True:
            chunk = await session.peek_process_output(
                process_id, after=output.end, limit=OUTPUT_PAGE_CHARS
            )
            output.add(chunk)
            if chunk.next >= goal:
                return "".join(output.text)

    async def stop(self, session_id: str, process_id: str) -> None:
        """Raises :class:`WorkspaceError` for an unknown command."""
        session = self._live(session_id)
        watch = self._watch(session_id)
        assert watch is not None
        watch.user_stops.add(process_id)
        await session.stop_process(process_id)
        await self._look_quietly(session_id)

    def _live(self, session_id: str) -> WorkspaceSession:
        session = self._deps.workspaces.get(session_id)
        if session is None:
            raise WorkspaceError("no live workspace session")
        return session

    async def _record(self, session_id: str, process_id: str) -> tuple[int, str] | None:
        folder: Path | None = self._deps.store.process_log_folder(session_id)
        if folder is None:
            return None
        found = await asyncio.to_thread(read_record, folder, process_id)
        return None if found is None else (found[0].base, found[1])


def _ended_by(watch: _Watch, status: ProcessStatus) -> EndedBy:
    if status.status == "exited":
        return "exit"
    return "user" if status.process_id in watch.user_stops else "model"
