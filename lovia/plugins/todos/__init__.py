"""Todo plugin: an externalized, per-turn-visible checklist for long tasks.

The first :class:`~lovia.plugins.Plugin`. See :func:`todos`.
"""

from __future__ import annotations

from .plugin import todos
from .store import TodoList, render_todos, todos_from_entries
from .types import Todo, TodoInput, TodoStatus

__all__ = [
    "Todo",
    "TodoInput",
    "TodoList",
    "TodoStatus",
    "render_todos",
    "todos",
    "todos_from_entries",
]
