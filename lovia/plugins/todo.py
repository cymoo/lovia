"""Todo plugin: an externalized checklist for long tasks.

``Todo`` is the plugin (a :class:`~lovia.plugins.Plugin`). Attach it to an agent::

    agent = Agent(name="builder", plugins=[Todo()], tools=[...])

``TodoItem`` is the per-item schema — both the model-facing tool input and the
host-side record (the list is full-replace, so there is no per-item id).

Observability: filter ``ToolCallCompleted`` where ``call.name`` matches the
configured ``tool_name`` (default ``"todo_write"``); ``ToolResultEntry.raw``
carries the structured ``list[TodoItem]``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from ..exceptions import UserError
from ..run_context import RunContext
from ..tools import Tool, tool
from ..transcript import (
    AssistantTextEntry,
    InputEntry,
    ReasoningEntry,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
)
from .base import PluginInstance, ViewInjector

TodoStatus = Literal["pending", "in_progress", "completed"]


class TodoItem(BaseModel):
    """One checklist item — both model input and host record."""

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


# -- store -------------------------------------------------------------------

_BOX = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def _parse_todo_items(arguments: str) -> list[TodoItem] | None:
    """Parse a ``todo_write`` call's JSON arguments into todo items.

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
        return [TodoItem.model_validate(item) for item in data["todos"]]
    except Exception:
        return None


def render_todos(items: list[TodoItem]) -> str:
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


def _normalize(items: list[TodoItem]) -> list[TodoItem]:
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
        self.items: list[TodoItem] = []

    def replace(self, inputs: list[TodoItem]) -> list[TodoItem]:
        """Replace the whole list (the only mutation). Returns the new items.

        Items are copied on the way in (normalization must not mutate the
        caller's objects) and the returned list is detached from the store.
        """
        self.items = _normalize([item.model_copy() for item in inputs])
        return list(self.items)

    def rehydrate_from(self, entries: list[TranscriptEntry], *, tool_name: str) -> None:
        """Rebuild from the newest valid ``todo_write`` call in ``entries``.

        Used after a resume (fresh empty store) or a handoff (the new agent's
        store starts empty but the prior agent's writes are in the transcript).
        A call whose arguments don't parse, or whose result is an error, never
        mutated the store, so the scan skips it and keeps looking.
        """
        found = _newest_write(entries, tool_name)
        if found is not None:
            self.replace(found[1])


def todos_from_entries(
    entries: list[TranscriptEntry], *, tool_name: str = "todo_write"
) -> list[TodoItem]:
    """Reconstruct the latest todo list from a transcript.

    Scans backward for the most recent ``tool_name`` call whose arguments parse
    as a todo-write payload and whose result is not an error. Returns an empty
    list when none is found. Used by
    the web layer to surface current todos on session reload.
    """
    store = TodoList()
    store.rehydrate_from(entries, tool_name=tool_name)
    return store.items


# -- plugin ------------------------------------------------------------------

_TOOL_DESCRIPTION = (
    "Maintain a structured todo list for the current task. Always pass the "
    "COMPLETE list — it replaces the previous one entirely. Each item has a "
    "short one-line `content`, a `status` (pending, in_progress, or completed), "
    "and an optional `active_form` (present-tense label shown while in_progress)."
)

_INSTRUCTIONS = (
    "## Todo\n"
    "Use the `todo_write` tool to externalize your plan when a task genuinely "
    "benefits from it — multi-step or complex work, or when the user asked for "
    "several things. Use your judgment: skip it for simple, single-step, or "
    "trivial requests, where a checklist only adds noise. When you do use it, "
    "capture the plan before starting, keep exactly one item in_progress at a "
    "time, and mark each item completed as soon as it is done."
)


def _render_result(result: list[TodoItem], ctx: RunContext[Any]) -> str:
    if not result:
        return "Todo list cleared."
    return "Updated todo list:\n" + render_todos(result)


def _make_tool(store: TodoList, tool_name: str) -> Tool:
    @tool(name=tool_name, description=_TOOL_DESCRIPTION, result_renderer=_render_result)
    async def todo_write(
        ctx: RunContext[Any],
        todos: Annotated[
            list[TodoItem],
            "The complete todo list. Replaces the previous list entirely.",
        ],
    ) -> list[TodoItem]:
        return store.replace(todos)

    return todo_write


# One call's output always comes in this order (``assemble_turn_entries`` in
# runtime/model_turn.py), so a step back in it starts a new call.
_OUTPUT_RANK = {ReasoningEntry: 0, AssistantTextEntry: 1, ToolCallEntry: 2}


def _newest_write(
    entries: list[TranscriptEntry], tool_name: str
) -> tuple[int, list[TodoItem]] | None:
    """The newest valid write in ``entries``: the model calls made after the
    one that wrote it, and its items. ``None`` when there is none.

    Valid means the arguments parse and the result is not an error (one still
    pending counts: the tool may have run). The transcript has no turn
    markers: a call's output is a run of reasoning, then text, then tool-call
    entries, so a new call starts after any other entry or where that order
    steps back. Derived from the transcript alone, the count spans runs,
    resumes and handoffs, and an overflow retry of the same call gets the
    same answer. One blind spot: a text-only reply followed by a run started
    with no new input whose reply opens with text — a call can itself carry
    several text entries, so those two count as one.
    """
    calls = 0
    later: int | None = None  # rank of the entry just after this one
    # Scanning backward, this holds each id's nearest result after the
    # current position (a provider may reuse call ids).
    failed: dict[str, bool] = {}
    for entry in reversed(entries):
        if isinstance(entry, ToolResultEntry):
            failed[entry.call_id] = entry.is_error
        rank = _OUTPUT_RANK.get(type(entry))
        if rank is not None and (later is None or later < rank):
            calls += 1  # this entry ends a call
        later = rank
        if (
            isinstance(entry, ToolCallEntry)
            and entry.name == tool_name
            and not failed.get(entry.call_id, False)
        ):
            items = _parse_todo_items(entry.arguments)
            if items is not None:
                return calls - 1, items  # minus the writing call itself
    return None


def _make_injector(store: TodoList, tool_name: str, every: int) -> ViewInjector:
    reconciled = False

    def inject(ctx: RunContext[Any]) -> list[TranscriptEntry] | None:
        nonlocal reconciled
        if not reconciled:
            reconciled = True
            if not store.items:
                store.rehydrate_from(ctx.entries, tool_name=tool_name)
        if all(t.status == "completed" for t in store.items):
            return None
        found = _newest_write(ctx.entries, tool_name)
        if found is None:
            return None
        since = found[0]
        if since < every or since % every:
            return None
        text = (
            "<system-reminder>\n"
            "The todo list hasn't been updated in a while. If you are still "
            "working through it, keep it current: exactly one item "
            "in_progress, finished items marked completed. If it no longer "
            "matches the work, rewrite or clear it. Don't mention this "
            "reminder to the user. Current list:\n"
            f"{render_todos(store.items)}\n"
            "</system-reminder>"
        )
        return [InputEntry(role="user", content=text)]

    return inject


@dataclass
class Todo:
    """The todo plugin: a ``todo_write`` tool + a staleness reminder.

    Each run, ``setup`` builds a fresh :class:`TodoList` and closes both the
    tool (which replaces the list) and the injector over it. The model sees
    its list in each write's tool result; the injector re-shows it only when
    unfinished items have gone ``remind_every`` model calls without a write
    (and again every ``remind_every`` calls after that).
    """

    tool_name: str = "todo_write"
    remind_every: int | None = 8
    """Model calls without a write before the list is re-shown; ``None``
    turns the reminder off."""
    instructions: str | None = _INSTRUCTIONS
    name: str = "todos"

    def __post_init__(self) -> None:
        every = self.remind_every
        if every is not None and (
            not isinstance(every, int) or isinstance(every, bool) or every < 1
        ):
            raise UserError(
                f"Todo(remind_every={every!r}) must be a positive int or None",
                hint="Use None to turn the reminder off.",
            )

    async def setup(self, ctx: RunContext[Any]) -> PluginInstance:
        store = TodoList()
        every = self.remind_every
        return PluginInstance(
            tools=[_make_tool(store, self.tool_name)],
            view_injectors=(
                [_make_injector(store, self.tool_name, every)]
                if every is not None
                else []
            ),
            instructions=self.instructions,
        )


__all__ = [
    "Todo",
    "TodoItem",
    "TodoList",
    "TodoStatus",
    "render_todos",
    "todos_from_entries",
]
