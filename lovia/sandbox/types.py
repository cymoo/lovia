"""Public data types for sandbox sessions and tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SandboxMode = Literal["readonly", "coding", "trusted"]


class FileContent(BaseModel):
    """Text returned by ``read_file``."""

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
    """A single directory entry."""

    name: str
    is_dir: bool
    size: int | None = None
    mtime: float | None = None


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


class SandboxSpec(BaseModel):
    """Configuration passed to sandbox backends."""

    root: str = "."
    mode: SandboxMode = "coding"
    env: dict[str, str] | None = None
    shell_timeout: float | None = 300.0
    max_read_chars: int = 50_000
    max_output_chars: int = 50_000


__all__ = [
    "CommandResult",
    "DirEntry",
    "EditResult",
    "FileChange",
    "FileContent",
    "SandboxMode",
    "SandboxSpec",
]
