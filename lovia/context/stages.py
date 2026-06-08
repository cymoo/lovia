"""Cheap transcript rewrites used before LLM-backed context compaction."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .._types import JsonObject
from ..transcript import InputEntry, ToolCallEntry, ToolResultEntry, TranscriptEntry
from ..transcript import safe_window

if TYPE_CHECKING:
    from .archive import CompactionArchive
    from .policy import PolicyContext


logger = logging.getLogger(__name__)

TOOL_RESULT_PLACEHOLDER = (
    "[Earlier tool result compacted. Re-run the tool if you need it.]"
)


@dataclass
class StageResult:
    """Result of one cheap context stage.

    Attributes:
        entries: Transcript entries after the stage has run.
        changed: Whether the stage changed the transcript.
        reason: Stable reason code, usually the stage ``name``.
        metadata: Extra stage-specific diagnostics.
    """

    entries: list[TranscriptEntry]
    changed: bool = False
    reason: str | None = None
    metadata: JsonObject = field(default_factory=dict)


class ContextStage(Protocol):
    """A cheap structural rewrite that runs before model calls."""

    name: str

    async def apply(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: "PolicyContext",
    ) -> StageResult:
        """Return a rewritten transcript, or the original when unchanged."""
        ...


class ToolResultBudgetStage:
    """Persist or preview large tool results before other stages erase them."""

    name = "tool_result_budget"

    def __init__(
        self,
        *,
        max_chars: int | None = 200_000,
        large_result_chars: int = 20_000,
        preview_chars: int = 2_000,
        archive: "CompactionArchive | None" = None,
    ) -> None:
        """Configure the tool-result character budget.

        Args:
            max_chars: Total allowed characters across tool-result outputs.
                ``None`` disables this stage.
            large_result_chars: Minimum size for an individual result to be
                replaced. Smaller outputs are left inline even if the total is
                over budget.
            preview_chars: Leading characters kept inline after replacement.
            archive: Optional sink for full outputs before they are replaced.
        """
        self.max_chars = max_chars
        self.large_result_chars = large_result_chars
        self.preview_chars = preview_chars
        self.archive = archive

    async def apply(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: "PolicyContext",
    ) -> StageResult:
        """Replace the largest eligible tool results until under budget."""
        if self.max_chars is None:
            return StageResult(entries=entries)

        tool_results = [
            (idx, entry)
            for idx, entry in enumerate(entries)
            if isinstance(entry, ToolResultEntry)
        ]
        total = sum(len(entry.output) for _, entry in tool_results)
        if total <= self.max_chars:
            return StageResult(entries=entries)

        new_entries = list(entries)
        archived: list[JsonObject] = []
        changed = False
        for idx, entry in sorted(
            tool_results, key=lambda pair: len(pair[1].output), reverse=True
        ):
            if total <= self.max_chars:
                break
            if len(entry.output) < self.large_result_chars:
                continue
            if _is_compacted_tool_result(entry.output):
                continue

            replacement, archive_meta = await self._replacement(entry, ctx=ctx)
            archived.append(archive_meta)
            new_entries[idx] = ToolResultEntry(
                call_id=entry.call_id,
                output=replacement,
                raw=None,
                is_error=entry.is_error,
            )
            total += len(replacement) - len(entry.output)
            changed = True

        if not changed:
            return StageResult(entries=entries)
        return StageResult(
            entries=new_entries,
            changed=True,
            reason=self.name,
            metadata={
                "tool_results": len(archived),
                "archives": archived,
                "remaining_tool_result_chars": total,
            },
        )

    async def _replacement(
        self,
        entry: ToolResultEntry,
        *,
        ctx: "PolicyContext",
    ) -> tuple[str, JsonObject]:
        """Return inline replacement text and archive metadata for ``entry``."""
        preview = entry.output[: self.preview_chars]
        if self.archive is None:
            return (
                _preview_marker(call_id=entry.call_id, preview=preview),
                {"call_id": entry.call_id, "archived": False},
            )

        try:
            ref = await self.archive.save_tool_result(
                entry.output,
                call_id=entry.call_id,
                ctx=ctx,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "context.archive.tool_result_failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return (
                _preview_marker(call_id=entry.call_id, preview=preview),
                {
                    "call_id": entry.call_id,
                    "archived": False,
                    "error": type(exc).__name__,
                },
            )

        return (
            _persisted_marker(call_id=entry.call_id, uri=ref.uri, preview=preview),
            {
                "call_id": entry.call_id,
                "archived": True,
                "uri": ref.uri,
            },
        )


class MiddleTrimStage:
    """Trim the middle of long transcripts while preserving tool pairs."""

    name = "middle_trim"

    def __init__(
        self,
        *,
        max_entries: int | None = 80,
        keep_initial: int = 3,
        keep_recent: int = 40,
    ) -> None:
        """Configure middle trimming.

        Args:
            max_entries: Maximum transcript length before trimming. ``None``
                disables this stage.
            keep_initial: Number of leading entries to keep.
            keep_recent: Number of recent trailing entries to keep.
        """
        self.max_entries = max_entries
        self.keep_initial = keep_initial
        self.keep_recent = keep_recent

    async def apply(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: "PolicyContext",
    ) -> StageResult:
        """Drop the middle of long transcripts using ``safe_window``."""
        del ctx
        if self.max_entries is None or len(entries) <= self.max_entries:
            return StageResult(entries=entries)

        head = max(0, self.keep_initial)
        tail = max(0, self.keep_recent)
        kept = safe_window(entries, head=head, tail=tail)
        if len(kept) >= len(entries):
            return StageResult(entries=entries)

        placeholder = InputEntry(
            role="user",
            content=f"[Snipped {len(entries) - len(kept)} earlier transcript entries.]",
        )
        new_entries = (
            [*kept[:head], placeholder, *kept[head:]]
            if _can_insert_user_entry(kept[:head])
            else kept
        )
        return StageResult(
            entries=new_entries,
            changed=True,
            reason=self.name,
            metadata={
                "entries_before": len(entries),
                "entries_after": len(new_entries),
                "snipped_entries": len(entries) - len(kept),
            },
        )


class ToolResultRetentionStage:
    """Replace older tool results with placeholders."""

    name = "tool_result_retention"

    def __init__(
        self,
        *,
        keep_recent: int | None = 3,
        min_chars: int = 120,
        placeholder: str = TOOL_RESULT_PLACEHOLDER,
    ) -> None:
        """Configure retention of recent tool results.

        Args:
            keep_recent: Number of latest tool results to keep intact. ``None``
                disables this stage; ``0`` allows all eligible results to be
                replaced.
            min_chars: Results at or below this size stay inline.
            placeholder: Text used for replaced older results.
        """
        self.keep_recent = keep_recent
        self.min_chars = min_chars
        self.placeholder = placeholder

    async def apply(
        self,
        entries: list[TranscriptEntry],
        *,
        ctx: "PolicyContext",
    ) -> StageResult:
        """Replace older long tool results with a compact placeholder."""
        del ctx
        if self.keep_recent is None:
            return StageResult(entries=entries)

        tool_results = [
            (idx, entry)
            for idx, entry in enumerate(entries)
            if isinstance(entry, ToolResultEntry)
        ]
        if len(tool_results) <= self.keep_recent:
            return StageResult(entries=entries)

        new_entries = list(entries)
        changed = 0
        old_results = (
            tool_results if self.keep_recent == 0 else tool_results[: -self.keep_recent]
        )
        for idx, entry in old_results:
            if len(entry.output) <= self.min_chars:
                continue
            if _is_compacted_tool_result(entry.output):
                continue
            new_entries[idx] = ToolResultEntry(
                call_id=entry.call_id,
                output=self.placeholder,
                raw=None,
                is_error=entry.is_error,
            )
            changed += 1

        if changed == 0:
            return StageResult(entries=entries)
        return StageResult(
            entries=new_entries,
            changed=True,
            reason=self.name,
            metadata={"tool_results": changed},
        )


def _is_compacted_tool_result(output: str) -> bool:
    """Return whether ``output`` already looks like a compacted tool result."""
    return (
        output.startswith(TOOL_RESULT_PLACEHOLDER)
        or output.startswith("[Persisted tool result]")
        or output.startswith("[Tool result preview]")
    )


def _preview_marker(*, call_id: str, preview: str) -> str:
    """Build the inline marker used when a result was not archived."""
    return (
        "[Tool result preview]\n"
        f"call_id: {call_id}\n"
        "Full output was too large for the active context and was not archived.\n\n"
        "Preview:\n"
        f"{preview}"
    )


def _persisted_marker(*, call_id: str, uri: str, preview: str) -> str:
    """Build the inline marker used when a full result was archived."""
    return (
        "[Persisted tool result]\n"
        f"call_id: {call_id}\n"
        f"full_output: {uri}\n\n"
        "Preview:\n"
        f"{preview}"
    )


def _can_insert_user_entry(entries: list[TranscriptEntry]) -> bool:
    """Return whether inserting a user entry would avoid splitting tool pairs."""
    pending_calls: set[str] = set()
    for entry in entries:
        if isinstance(entry, ToolCallEntry):
            pending_calls.add(entry.call_id)
        elif isinstance(entry, ToolResultEntry):
            pending_calls.discard(entry.call_id)
        elif isinstance(entry, InputEntry) and pending_calls:
            return False
    return not pending_calls
