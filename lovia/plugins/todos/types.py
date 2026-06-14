"""Data types for the todo plugin.

``Todo`` is the host-side record; ``TodoInput`` is the model-facing schema for
one item in the ``todo_write`` array. The list is full-replace, so there is no
per-item id — the list *is* the state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

TodoStatus = Literal["pending", "in_progress", "completed"]


@dataclass
class Todo:
    """One checklist item. ``active_form`` is the present-tense label shown
    while the item is ``in_progress`` (e.g. ``"Running the test suite"``)."""

    content: str
    status: TodoStatus = "pending"
    active_form: str | None = None


class TodoInput(BaseModel):
    """One item as written by the model via ``todo_write``."""

    content: str = Field(
        description=(
            "Short, one-line imperative label for the task, e.g. 'Run the test "
            "suite'. Keep it glanceable — a few words, not a paragraph."
        )
    )
    status: TodoStatus = Field(
        default="pending",
        description="One of: pending, in_progress, completed.",
    )
    active_form: str | None = Field(
        default=None,
        description=(
            "Present-tense form shown while in_progress, e.g. 'Running the test suite'."
        ),
    )


__all__ = ["Todo", "TodoInput", "TodoStatus"]
