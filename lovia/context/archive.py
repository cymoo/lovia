"""Archive sinks used by context compaction."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Protocol

from ..transcript import TranscriptEntry, entry_to_dict

if TYPE_CHECKING:
    from .policy import PolicyContext


ArchiveRoot = str | Path | Callable[["PolicyContext"], str | Path]


@dataclass
class ArchiveRef:
    """Reference to data saved outside the active model context."""

    uri: str
    kind: Literal["transcript", "tool_result"]
    metadata: dict[str, object] = field(default_factory=dict)


class CompactionArchive(Protocol):
    """Write-only sink for data removed from the active transcript."""

    async def save_transcript(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: "PolicyContext",
        reason: str,
    ) -> ArchiveRef: ...

    async def save_tool_result(
        self,
        output: str,
        *,
        call_id: str,
        ctx: "PolicyContext",
    ) -> ArchiveRef: ...


class FileCompactionArchive:
    """Store compacted transcripts and large tool outputs on disk.

    ``root`` may be a static path or a callable that derives the path from
    the current :class:`PolicyContext`.
    """

    def __init__(self, root: ArchiveRoot = ".lovia") -> None:
        self.root = root

    async def save_transcript(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: "PolicyContext",
        reason: str,
    ) -> ArchiveRef:
        session = _safe_segment(ctx.session_id or "default")
        run = _safe_segment(ctx.run_id or "run")
        path = (
            self._root_for(ctx)
            / "transcripts"
            / session
            / f"{run}-{time.time_ns()}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry_to_dict(entry), ensure_ascii=False))
                f.write("\n")
        return ArchiveRef(
            uri=str(path),
            kind="transcript",
            metadata={
                "entries": len(entries),
                "reason": reason,
                "session_id": ctx.session_id,
                "run_id": ctx.run_id,
            },
        )

    async def save_tool_result(
        self,
        output: str,
        *,
        call_id: str,
        ctx: "PolicyContext",
    ) -> ArchiveRef:
        session = _safe_segment(ctx.session_id or "default")
        safe_call = _safe_segment(call_id or "tool-result")
        path = (
            self._root_for(ctx)
            / "tool-results"
            / session
            / f"{safe_call}-{time.time_ns()}.txt"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        return ArchiveRef(
            uri=str(path),
            kind="tool_result",
            metadata={
                "call_id": call_id,
                "chars": len(output),
                "session_id": ctx.session_id,
                "run_id": ctx.run_id,
            },
        )

    def _root_for(self, ctx: "PolicyContext") -> Path:
        root = self.root(ctx) if callable(self.root) else self.root
        return Path(root)


def _safe_segment(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in value)
    return safe or "default"
