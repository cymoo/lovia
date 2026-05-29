"""Public types shared by workspace backends and tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "AuditVerdict",
    "DirEntry",
    "ExecLimits",
    "ExecResult",
]


@dataclass(frozen=True)
class ExecLimits:
    """Per-call execution limits."""

    timeout: float | None = 120.0
    max_output_bytes: int = 50_000


@dataclass(frozen=True)
class ExecResult:
    """Outcome of a single command."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class DirEntry:
    """A single directory entry."""

    name: str
    is_dir: bool
    size: int | None = None
    mtime: float | None = None


AuditVerdict = Literal["pass", "warn", "block"]
