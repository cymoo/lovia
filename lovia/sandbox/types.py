"""Public types shared by the sandbox layer.

All dataclasses are frozen and importable without pulling in any provider.
Downstream packages (web UI, custom Provider impls) can depend on these
without dragging in concrete sandbox machinery.
"""

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
    """Per-call execution limits applied to :meth:`Sandbox.exec`.

    ``timeout`` bounds wall-clock; ``max_output_bytes`` clips stdout and
    stderr independently. When clipped, the rest of the stream is still
    drained so the child doesn't stall, and the result is flagged
    ``truncated=True``.
    """

    timeout: float | None = 30.0
    max_output_bytes: int = 64_000


@dataclass(frozen=True)
class ExecResult:
    """Outcome of a single :meth:`Sandbox.exec` call.

    ``timed_out`` is True when ``timeout`` was hit; ``exit_code`` is then
    ``-1`` and the streams carry whatever was captured before the process
    was killed. ``truncated`` is True when ``max_output_bytes`` clipped
    either stream.
    """

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
    # Provider-specific bag — Docker fills in container_id, K8s pod_name,
    # etc. Stays opaque to core consumers.
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class DirEntry:
    """A single entry returned by :meth:`Sandbox.ls`."""

    name: str
    is_dir: bool
    size: int | None = None
    # POSIX modify-time, seconds since epoch, when the impl can cheaply
    # produce it. ``None`` means "not provided".
    mtime: float | None = None


# AuditPolicy verdict. ``warn`` runs the tool and annotates the result;
# ``block`` raises an :class:`AuditBlocked` the runner converts into a
# model-visible error.
AuditVerdict = Literal["pass", "warn", "block"]
