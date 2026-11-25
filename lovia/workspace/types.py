"""Public data types for workspace sessions and tools.

Plain dataclasses, not pydantic models like the rest of lovia's data types:
the session engine that builds them imports nothing outside the standard
library (see ``tests/workspace/test_engine_stdlib.py``), so that it can run
where lovia's dependencies are not installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, TypeVar

WorkspaceMode = Literal["readonly", "coding", "trusted"]

_ItemT = TypeVar("_ItemT")


@dataclass(frozen=True, kw_only=True)
class FileContent:
    """Text returned by ``read_text``."""

    path: str
    content: str
    start: int
    end: int
    total_lines: int
    truncated: bool = False


@dataclass(frozen=True, kw_only=True)
class FileBytes:
    """Raw bytes returned by ``read_bytes`` (binary payloads, e.g. images)."""

    path: str
    data: bytes


@dataclass(frozen=True, kw_only=True)
class FileChange:
    """Result of creating or updating a file."""

    ok: bool = True
    path: str
    action: Literal["created", "updated", "unchanged"]
    bytes_written: int = 0
    message: str | None = None


@dataclass(frozen=True, kw_only=True)
class EditResult:
    """Result of an exact text edit."""

    ok: bool
    path: str
    replacements: int = 0
    changed: bool = False
    message: str | None = None


@dataclass(frozen=True, kw_only=True)
class DirEntry:
    """One entry returned by ``list_files``.

    ``path`` is workspace-relative when the entry is inside the workspace
    root, absolute otherwise. ``symlink_target`` carries the resolved target
    when the entry is a symlink, so listings show where a link leads before
    anything tries to read it.
    """

    path: str
    is_dir: bool
    size: int | None = None
    mtime: float | None = None
    symlink_target: str | None = None


@dataclass(frozen=True, kw_only=True)
class GrepMatch:
    """One matching line returned by ``grep``."""

    path: str
    line: int
    text: str


@dataclass(frozen=True, kw_only=True)
class CommandResult:
    """Outcome of a one-shot shell command."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True, kw_only=True)
class ProcessStart:
    """A background process just started by ``spawn``."""

    process_id: str
    pid: int
    command: str


@dataclass(frozen=True, kw_only=True)
class ProcessOutput:
    """One incremental read of a background process.

    ``output`` is everything the process wrote (stdout and stderr merged)
    since the previous read; ``truncated`` reports that older unread output
    was left out, over the cap of one read. After the process ends, ``status`` and
    ``exit_code`` carry the outcome — reads never turn into errors.
    """

    process_id: str
    status: Literal["running", "exited", "killed"]
    exit_code: int | None = None
    output: str = ""
    truncated: bool = False


@dataclass(frozen=True, kw_only=True)
class ProcessStatus:
    """Passive status of one background process — consumes no output.

    ``exit_seen`` reports whether a ``read_process_output``/``kill_process``
    already delivered the exit to its caller; the per-turn status reminder
    announces an exited process until that flips.

    Times are Unix seconds by the clock of the machine the process runs on.
    """

    process_id: str
    command: str
    status: Literal["running", "exited", "killed"]
    exit_code: int | None = None
    exit_seen: bool = False
    cwd: str = ""
    """The directory it started in: absolute, POSIX."""
    started_at: float = 0.0
    ended_at: float | None = None
    """When the exit was noticed."""
    output_chars: int = 0
    """Characters written so far: the offset the next one will have."""
    output_closed: bool = False
    """Whether nothing more can be written. Later than the exit while a
    process it started still holds the output."""
    last_line: str = ""
    """The last line of output that is not blank, clipped."""


@dataclass(frozen=True, kw_only=True)
class ProcessChunk:
    """One page of a background process's output, read without consuming it.

    Offsets count characters from the start of the process.
    """

    process_id: str
    text: str
    base: int
    """The oldest offset still held. Past the one asked for: what lies
    between was dropped, and ``text`` starts here."""
    next: int
    """The offset to read from next."""
    eof: bool
    """The output is closed and ``next`` is its end. This follows the output,
    not the process, which may exit first."""


@dataclass(frozen=True, kw_only=True)
class RootFacts:
    """What the workspace prompt says about the root beyond its policy —
    read from the filesystem, so a session reports it for where it runs."""

    name: str
    git: bool = False
    python: Literal["uv", "poetry", "pip"] = "pip"
    system: str = ""
    """The OS there, as ``platform.system()`` names it (``""``: not known).
    Shell commands run under cmd.exe on ``"Windows"``, ``/bin/sh`` elsewhere."""


@dataclass(frozen=True, kw_only=True)
class InstructionFile:
    """One project instructions file (``AGENTS.md``) found for a root."""

    path: str
    """Where it sits on the way up: absolute, POSIX, not a link's target."""
    level: int
    """Directories above the root it sits in: 0 is the root's own."""
    text: str = ""
    skipped: str = ""
    """Why it is left out; empty when ``text`` is in use."""


@dataclass(frozen=True)
class WorkspaceLimits:
    """Size and count caps for the workspace tools — one discoverable home.

    These shape what a *tool* returns to the model (the "tool layer"): read
    pagination, shell-output clipping, grep skip/clip thresholds, and listing
    caps. That is a different concern from the runner's
    ``Agent.max_tool_output_chars``, which is a transcript/storage backstop
    applied to every tool's rendered output regardless of source.

    Two different axes, hence the very different magnitudes: ``*_chars`` cap
    how much text is returned to the model (a context budget; results are
    paginated/clipped and flagged ``truncated``), while ``*_bytes`` cap how
    much is loaded from disk (a memory/OOM guard).
    """

    max_file_read_chars: int = 50_000
    """Max characters returned by one ``read_file`` call (drives pagination;
    the result is flagged ``truncated``)."""

    max_file_read_bytes: int = 10_000_000
    """Files larger than this are read only up to a bounded prefix, so a
    huge file can't exhaust memory."""

    max_shell_output_chars: int = 30_000
    """Max characters of one command's stdout/stderr captured (each stream
    clipped independently). Also what one ``read_process_output`` returns at
    most: the newest this-many of the characters not read yet."""

    max_grep_file_bytes: int = 5_000_000
    """Files larger than this are skipped by grep."""

    max_grep_line_chars: int = 400
    """Each matched grep line is clipped to this."""

    max_list_results: int = 500
    """Default cap on entries returned by ``list_files``."""

    max_grep_matches: int = 100
    """Default cap on matches returned by ``grep``."""

    max_process_buffer_chars: int = 1_000_000
    """Characters of output kept per background process, newest first, until
    a minute after it has ended and closed its output. From then on only what
    the model has not read is kept, up to ``max_shell_output_chars``."""


class ClippedList(list[_ItemT]):
    """A list result that also reports whether entries were dropped to a cap.

    It *is* a ``list`` (iterate / index / compare it normally); the extra
    ``truncated`` flag lets a renderer tell the model that results were capped
    — the listing/search counterpart to ``FileContent.truncated`` — instead of
    the session raising and leaving the model empty-handed.
    """

    truncated: bool

    def __init__(
        self, items: Iterable[_ItemT] = (), *, truncated: bool = False
    ) -> None:
        super().__init__(items)
        self.truncated = truncated


__all__ = [
    "ClippedList",
    "CommandResult",
    "DirEntry",
    "EditResult",
    "FileChange",
    "FileContent",
    "GrepMatch",
    "ProcessChunk",
    "ProcessOutput",
    "ProcessStart",
    "ProcessStatus",
    "WorkspaceLimits",
    "WorkspaceMode",
]
