"""Lightweight todo-list tools backed by an in-memory ``TodoList``.

::

    from lovia.builtins.todo import TodoList, todo_tools

    todos = TodoList()
    agent = Agent(name="x", tools=todo_tools(todos))

The :class:`TodoList` instance is yours — read or render it however you
like (e.g. emit it into the system prompt via ``@agent.system_prompt``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal

from ..exceptions import ToolError
from ..tools import Tool, tool


Status = Literal["pending", "in_progress", "done", "blocked"]


@dataclass
class Todo:
    id: str
    title: str
    status: Status = "pending"
    note: str = ""


@dataclass
class TodoList:
    """Plain in-memory ordered todo list, addressable by id."""

    items: list[Todo] = field(default_factory=list)

    def add(self, title: str, *, note: str = "", id: str | None = None) -> Todo:
        tid = id or f"t{len(self.items) + 1}"
        if any(t.id == tid for t in self.items):
            raise ToolError(f"Todo id already exists: {tid}")
        todo = Todo(id=tid, title=title, note=note)
        self.items.append(todo)
        return todo

    def update(
        self,
        id: str,
        *,
        status: Status | None = None,
        title: str | None = None,
        note: str | None = None,
    ) -> Todo:
        for t in self.items:
            if t.id == id:
                if status is not None:
                    t.status = status
                if title is not None:
                    t.title = title
                if note is not None:
                    t.note = note
                return t
        raise ToolError(f"Unknown todo id: {id}")

    def render(self) -> str:
        if not self.items:
            return "(no todos)"
        marker = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "blocked": "[!]"}
        return "\n".join(
            f"{marker[t.status]} {t.id} {t.title}" + (f"  — {t.note}" if t.note else "")
            for t in self.items
        )


def todo_tools(state: TodoList) -> list[Tool]:
    """Build the three todo tools (``add_todo``, ``update_todo``, ``list_todos``)
    wired to a shared :class:`TodoList`.
    """

    @tool
    def add_todo(
        title: Annotated[str, "Short todo title."],
        note: Annotated[str, "Optional details."] = "",
    ) -> str:
        """Append a todo. Returns the assigned id."""
        return state.add(title, note=note).id

    @tool
    def update_todo(
        id: Annotated[str, "Todo id."],
        status: Annotated[
            Status | None,
            "New status (pending/in_progress/done/blocked).",
        ] = None,
        title: Annotated[str | None, "New title."] = None,
        note: Annotated[str | None, "New note."] = None,
    ) -> str:
        """Update fields on a todo. Returns its rendered line."""
        t = state.update(id, status=status, title=title, note=note)
        return f"{t.id} -> {t.status}: {t.title}"

    @tool
    def list_todos() -> str:
        """Render the current todo list."""
        return state.render()

    return [add_todo, update_todo, list_todos]
