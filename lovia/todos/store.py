"""The run-scoped todo store and its rendering.

``TodoList`` is the source of truth *during* a live run. It is rebuilt fresh per
run (so concurrent runs never share it) and, after a resume or a handoff, can
recover its state from the transcript via :meth:`rehydrate_from` — the last
``todo_write`` call's arguments are the durable record (the transcript is
checkpointed and session-persisted). The list is full-replace: each
``todo_write`` swaps the whole list, so the store is just ``fold(todo_write)``.
"""

from __future__ import annotations

import json
import logging

from ..transcript import ToolCallEntry, TranscriptEntry
from .types import Todo, TodoInput

logger = logging.getLogger(__name__)

_BOX = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def render_todos(items: list[Todo]) -> str:
    """Render a checklist string for the model / a reminder block."""
    lines: list[str] = []
    for t in items:
        label = (
            t.active_form
            if (t.status == "in_progress" and t.active_form)
            else t.content
        )
        lines.append(f"{_BOX.get(t.status, '[ ]')} {label}")
    return "\n".join(lines)


def _normalize(items: list[Todo]) -> list[Todo]:
    """Soft rule: at most one ``in_progress`` item. Extras are demoted to
    ``pending`` rather than rejected, so a sloppy model write still applies."""
    seen_active = False
    for t in items:
        if t.status == "in_progress":
            if seen_active:
                t.status = "pending"
            else:
                seen_active = True
    return items


class TodoList:
    """In-memory, run-scoped todo store."""

    def __init__(self) -> None:
        self.items: list[Todo] = []

    def replace(self, inputs: list[TodoInput]) -> list[Todo]:
        """Replace the whole list (the only mutation). Returns the new items."""
        self.items = _normalize(
            [
                Todo(content=i.content, status=i.status, active_form=i.active_form)
                for i in inputs
            ]
        )
        return self.items

    def rehydrate_from(
        self, entries: list[TranscriptEntry], *, tool_name: str
    ) -> None:
        """Rebuild from the most recent ``todo_write`` call in ``entries``.

        Used after a resume (fresh empty store) or a handoff (the new agent's
        store starts empty but the prior agent's writes are in the transcript).
        Reads ``ToolCallEntry.arguments`` — the model's JSON string, always
        preserved across resume paths — rather than ``ToolResultEntry.raw``,
        which the session-resume path drops.
        """
        for entry in reversed(entries):
            if isinstance(entry, ToolCallEntry) and entry.name == tool_name:
                try:
                    data = json.loads(entry.arguments)
                    raw = data.get("todos", []) if isinstance(data, dict) else []
                    inputs = [TodoInput.model_validate(item) for item in raw]
                except Exception:
                    logger.warning("todo rehydrate failed", exc_info=True)
                    return
                self.replace(inputs)
                return


__all__ = ["TodoList", "render_todos"]
