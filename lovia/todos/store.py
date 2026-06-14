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

from ..transcript import ToolCallEntry, TranscriptEntry
from .types import Todo, TodoInput

_BOX = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def _parse_todo_inputs(arguments: str) -> list[TodoInput] | None:
    """Parse a ``todo_write`` call's JSON arguments into todo inputs.

    Returns ``None`` when the payload is not a todo write (no ``todos`` array)
    or fails validation — callers treat that as "not a todo call".
    """
    try:
        data = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return None
    if not (isinstance(data, dict) and isinstance(data.get("todos"), list)):
        return None
    try:
        return [TodoInput.model_validate(item) for item in data["todos"]]
    except Exception:
        return None


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
                inputs = _parse_todo_inputs(entry.arguments)
                if inputs is not None:
                    self.replace(inputs)
                return


def todos_from_entries(entries: list[TranscriptEntry]) -> list[Todo]:
    """Reconstruct the latest todo list from a transcript, name-agnostically.

    Scans backward for the most recent tool call whose arguments parse as a
    todo-write payload (a ``todos`` array), so it works even if the tool was
    renamed. Returns an empty list when none is found. Used by the web layer to
    surface current todos on session reload.
    """
    for entry in reversed(entries):
        if isinstance(entry, ToolCallEntry):
            inputs = _parse_todo_inputs(entry.arguments)
            if inputs is not None:
                return TodoList().replace(inputs)
    return []


__all__ = ["TodoList", "render_todos", "todos_from_entries"]
