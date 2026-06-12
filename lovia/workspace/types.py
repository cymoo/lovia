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


def clip_text(
    text: str,
    limit: int,
    *,
    hint: str = "",
    keep_tail: bool = False,
) -> tuple[str, bool]:
    """Clip ``text`` to roughly ``limit`` characters with an explicit notice.

    Returns ``(clipped_text, truncated)``. When ``keep_tail`` is True the
    head and tail halves are kept with the notice in between (useful for
    shell output where exit diagnostics sit at the end); otherwise only the
    head is kept and the notice is appended.
    """
    if len(text) <= limit:
        return text, False
    omitted_note = f"showing {limit} of {len(text)} chars"
    notice = f"\n[... truncated: {omitted_note}.{' ' + hint if hint else ''}]\n"
    if limit <= 0:
        return notice.strip(), True
    if keep_tail:
        head = limit // 2
        tail = limit - head
        return text[:head] + notice + text[-tail:], True
    return text[:limit] + notice.rstrip("\n"), True


__all__ = [
    "CommandResult",
    "DirEntry",
    "EditResult",
    "FileChange",
    "FileContent",
    "GrepMatch",
    "WorkspaceMode",
    "clip_text",
]
