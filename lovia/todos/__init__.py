"""Todo plugin: an externalized, per-turn-visible checklist for long tasks.

The first :class:`~lovia.plugins.Plugin`. See :func:`todo_plugin`.
"""

from __future__ import annotations

from .plugin import todo_plugin
from .store import TodoList, render_todos
from .types import Todo, TodoInput, TodoStatus

__all__ = [
    "Todo",
    "TodoInput",
    "TodoList",
    "TodoStatus",
    "render_todos",
    "todo_plugin",
]
