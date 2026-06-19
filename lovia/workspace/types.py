"""Public data types for workspace sessions and tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, TypeVar

from pydantic import BaseModel, Field

WorkspaceMode = Literal["readonly", "coding", "trusted"]

_ItemT = TypeVar("_ItemT")


class FileContent(BaseModel):
    """Text returned by ``read_text``."""

    path: str
    content: str
    start: int = Field(ge=1)
    end: int = Field(ge=0)
    total_lines: int
    truncated: bool = False


class FileChange(BaseModel):
    """Result of creating or updating a file."""

    ok: bool = True
    path: str
    action: Literal["created", "updated", "unchanged"]
    bytes_written: int = 0
    message: str | None = None


class EditResult(BaseModel):
    """Result of an exact text edit."""

    ok: bool
    path: str
    replacements: int = 0
    changed: bool = False
    message: str | None = None


class DirEntry(BaseModel):
    """One entry returned by ``list_files``.

    ``path`` is workspace-relative; for a plain directory listing it is the
    entry's path under the listed directory, for a pattern match it is the
    full matched path.
    """

    path: str
    is_dir: bool
    size: int | None = None
    mtime: float | None = None


class GrepMatch(BaseModel):
    """One matching line returned by ``grep``."""

    path: str
    line: int = Field(ge=1)
    text: str


class CommandResult(BaseModel):
    """Outcome of a one-shot shell command."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class WorkspaceLimits:
    """Size and count caps for the workspace tools — one discoverable home.

    These shape what a *tool* returns to the model (the "tool layer"): read
    pagination, shell-output clipping, grep skip/clip thresholds, and listing
    caps. That is a different concern from the runner's
    ``Agent.max_tool_output_chars``, which is a transcript/storage backstop
    applied to every tool's rendered output regardless of source.

    Attributes:
        max_file_read_chars: Max characters returned by one ``read_file`` call
            (drives pagination; the result is flagged ``truncated``).
        max_file_read_bytes: Files larger than this are read only up to a
            bounded prefix, so a huge file can't exhaust memory.
        max_shell_output_chars: Max characters of one command's stdout/stderr
            captured (each stream clipped independently).
        max_grep_file_bytes: Files larger than this are skipped by grep.
        max_grep_line_chars: Each matched grep line is clipped to this.
        max_list_results: Default cap on entries returned by ``list_files``.
        max_grep_matches: Default cap on matches returned by ``grep``.
    """

    max_file_read_chars: int = 50_000
    max_file_read_bytes: int = 10_000_000
    max_shell_output_chars: int = 30_000
    max_grep_file_bytes: int = 5_000_000
    max_grep_line_chars: int = 400
    max_list_results: int = 500
    max_grep_matches: int = 100


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
    "WorkspaceLimits",
    "WorkspaceMode",
]
