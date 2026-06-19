"""Public data types for workspace sessions and tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

WorkspaceMode = Literal["readonly", "coding", "trusted"]


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


__all__ = [
    "CommandResult",
    "DirEntry",
    "EditResult",
    "FileChange",
    "FileContent",
    "GrepMatch",
    "WorkspaceMode",
]
