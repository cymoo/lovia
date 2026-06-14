"""The todo plugin: a ``todo_write`` tool + a per-turn reminder injector.

``todos()`` returns a :class:`~lovia.plugins.Plugin`. Each run, ``setup`` builds
a fresh :class:`~lovia.plugins.todos.store.TodoList` and closes both the tool
(which replaces the list) and the injector (which re-shows it every turn) over
it. Attach it to an agent::

    agent = Agent(name="builder", plugins=[todos()], tools=[...])

Observability: filter ``ToolCallCompleted`` where ``call.name == "todo_write"``;
``ToolResultEntry.raw`` carries the structured ``list[Todo]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from ..base import Plugin, PluginInstance, ViewInjector
from ...run_context import RunContext
from ...tools import Tool, tool
from ...transcript import InputEntry, TranscriptEntry
from .store import TodoList, render_todos
from .types import Todo, TodoInput

# Mechanics only — how the tool behaves. The policy for *when* to use it lives in
# the system-prompt instructions below, so the two don't duplicate each other.
_TOOL_DESCRIPTION = (
    "Maintain a structured todo list for the current task. Always pass the "
    "COMPLETE list — it replaces the previous one entirely. Each item has a "
    "short one-line `content`, a `status` (pending, in_progress, or completed), "
    "and an optional `active_form` (present-tense label shown while in_progress)."
)

# Policy — *when* to use it, left to the model's judgment so simple tasks don't
# get spurious checklists.
_INSTRUCTIONS = (
    "Use the `todo_write` tool to externalize your plan when a task genuinely "
    "benefits from it — multi-step or complex work, or when the user asked for "
    "several things. Use your judgment: skip it for simple, single-step, or "
    "trivial requests, where a checklist only adds noise. When you do use it, "
    "capture the plan before starting, keep exactly one item in_progress at a "
    "time, and mark each item completed as soon as it is done. Your current "
    "list is shown back to you each turn."
)


def _make_tool(store: TodoList, tool_name: str) -> Tool:
    def render_result(result: list[Todo], ctx: RunContext) -> str:
        if not result:
            return "Todo list cleared."
        return "Updated todo list:\n" + render_todos(result)

    @tool(name=tool_name, description=_TOOL_DESCRIPTION, result_renderer=render_result)
    async def todo_write(
        ctx: RunContext,
        todos: Annotated[
            list[TodoInput],
            "The complete todo list. Replaces the previous list entirely.",
        ],
    ) -> list[Todo]:
        return store.replace(todos)

    return todo_write


def _make_injector(store: TodoList, tool_name: str) -> ViewInjector:
    reconciled = False

    def inject(ctx: RunContext) -> list[TranscriptEntry] | None:
        nonlocal reconciled
        # Once per run, reconcile an empty store with the transcript so a resumed
        # or handed-off run recovers prior todos without core persistence. After
        # that the store is authoritative (the tool keeps it current), so we
        # never rescan — important for long runs that never use todos.
        if not reconciled:
            reconciled = True
            if not store.items:
                store.rehydrate_from(ctx.entries, tool_name=tool_name)
        if not store.items:
            return None
        text = (
            "<system-reminder>\n"
            "Your current todo list (keep it updated; exactly one item should "
            "be in_progress):\n"
            f"{render_todos(store.items)}\n"
            "</system-reminder>"
        )
        return [InputEntry(role="user", content=text)]

    return inject


@dataclass
class _TodoPlugin:
    tool_name: str = "todo_write"
    inject: bool = True
    instructions: str | None = _INSTRUCTIONS
    name: str = "todos"  # plugin identity (satisfies the Plugin protocol)

    async def setup(self) -> PluginInstance:
        store = TodoList()
        return PluginInstance(
            tools=[_make_tool(store, self.tool_name)],
            view_injectors=(
                [_make_injector(store, self.tool_name)] if self.inject else []
            ),
            instructions=self.instructions,
        )


def todos(
    *,
    tool_name: str = "todo_write",
    inject: bool = True,
    instructions: str | None = _INSTRUCTIONS,
) -> Plugin:
    """Build the todo plugin.

    Args:
        tool_name: Name the tool is exposed under (default ``"todo_write"``).
        inject: When ``True`` (default), re-show the current list to the model
            every turn via a transient ``<system-reminder>`` (never persisted).
        instructions: System-prompt guidance, or ``None`` to omit it and rely on
            the tool description alone.
    """
    return _TodoPlugin(tool_name=tool_name, inject=inject, instructions=instructions)


__all__ = ["todos"]
