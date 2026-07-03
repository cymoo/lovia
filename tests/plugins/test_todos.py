"""Tests for the todo plugin: store semantics, rendering, end-to-end re-injection."""

from __future__ import annotations

import json

import pytest

from lovia import Agent, Runner, Todo
from lovia.plugins.todo import TodoItem, TodoList, render_todos, todos_from_entries
from lovia.transcript import InputEntry, ToolCallEntry

from ..scripted_provider import ScriptedProvider, call, text


def _inputs(*items: dict) -> list[TodoItem]:
    return [TodoItem.model_validate(i) for i in items]


def test_replace_is_full_overwrite() -> None:
    store = TodoList()
    store.replace(_inputs({"content": "a"}, {"content": "b"}))
    assert [t.content for t in store.items] == ["a", "b"]
    store.replace(_inputs({"content": "c"}))
    assert [t.content for t in store.items] == ["c"]


def test_normalize_keeps_one_in_progress() -> None:
    store = TodoList()
    store.replace(
        _inputs(
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
            {"content": "c", "status": "in_progress"},
        )
    )
    statuses = [t.status for t in store.items]
    assert statuses.count("in_progress") == 1
    assert statuses == ["in_progress", "pending", "pending"]


def test_render_shows_active_form_for_in_progress() -> None:
    items = [
        TodoItem(content="Read routes", status="completed"),
        TodoItem(content="Implement", status="in_progress", active_form="Implementing"),
        TodoItem(content="Test", status="pending"),
    ]
    rendered = render_todos(items)
    assert "[x] Read routes" in rendered
    assert "[~] Implementing" in rendered  # active_form, not content
    assert "[ ] Test" in rendered


def test_rehydrate_from_transcript() -> None:
    args = {"todos": [{"content": "x"}, {"content": "y", "status": "completed"}]}
    transcript = [
        ToolCallEntry(call_id="c1", name="todo_write", arguments=json.dumps(args))
    ]
    store = TodoList()
    assert store.items == []
    store.rehydrate_from(transcript, tool_name="todo_write")
    assert [t.content for t in store.items] == ["x", "y"]
    assert store.items[1].status == "completed"


def test_rehydrate_uses_latest_call() -> None:
    older = {"todos": [{"content": "old"}]}
    newer = {"todos": [{"content": "new"}]}
    transcript = [
        ToolCallEntry(call_id="c1", name="todo_write", arguments=json.dumps(older)),
        ToolCallEntry(call_id="c2", name="todo_write", arguments=json.dumps(newer)),
    ]
    store = TodoList()
    store.rehydrate_from(transcript, tool_name="todo_write")
    assert [t.content for t in store.items] == ["new"]


def test_rehydrate_tolerates_garbage() -> None:
    transcript = [ToolCallEntry(call_id="c1", name="todo_write", arguments="not json")]
    store = TodoList()
    store.rehydrate_from(transcript, tool_name="todo_write")  # no raise
    assert store.items == []


def test_rehydrate_skips_malformed_latest_write() -> None:
    # A malformed call never mutated the store, so the newest *valid* write is
    # still the current state — the scan must not stop at the malformed one.
    valid = {"todos": [{"content": "keep me"}]}
    transcript = [
        ToolCallEntry(call_id="c1", name="todo_write", arguments=json.dumps(valid)),
        ToolCallEntry(call_id="c2", name="todo_write", arguments="not json"),
        ToolCallEntry(
            call_id="c3",
            name="todo_write",
            # JSON-valid but fails TodoItem validation.
            arguments=json.dumps({"todos": [{"status": 123}]}),
        ),
    ]
    store = TodoList()
    store.rehydrate_from(transcript, tool_name="todo_write")
    assert [t.content for t in store.items] == ["keep me"]
    # The web-layer view agrees with the injector-side rehydration.
    assert [t.content for t in todos_from_entries(transcript)] == ["keep me"]


@pytest.mark.asyncio
async def test_end_to_end_reinjection_and_audit_trail() -> None:
    first = {
        "todos": [
            {"content": "Plan", "status": "in_progress", "active_form": "Planning"},
            {"content": "Build"},
        ]
    }
    second = {
        "todos": [
            {"content": "Plan", "status": "completed"},
            {"content": "Build", "status": "in_progress", "active_form": "Building"},
        ]
    }
    provider = ScriptedProvider(
        [
            call("todo_write", first, call_id="c1"),
            call("todo_write", second, call_id="c2"),
            text("all done"),
        ]
    )
    agent = Agent(name="t", model=provider, plugins=[Todo()])
    result = await Runner.run(agent, "do the thing")

    # Turn 1's view has no reminder (store still empty); turns 2 and 3 do.
    def has_reminder(turn) -> bool:
        return any(
            m.role == "user" and "system-reminder" in (m.content or "") for m in turn
        )

    assert not has_reminder(provider.calls[0])
    assert has_reminder(provider.calls[1])
    assert has_reminder(provider.calls[2])
    # The reminder reflects the latest write (Building shown via active_form).
    assert any("Building" in (m.content or "") for m in provider.calls[2])

    # Reminders are never persisted...
    assert not any(
        isinstance(e, InputEntry) and "system-reminder" in (e.content or "")
        for e in result.entries
    )
    # ...but every todo_write call+result IS in the transcript (audit trail),
    # and the result preserves the structured todos in ``raw``.
    todo_results = [e for e in result.entries if e.type == "tool_result"]
    assert len(todo_results) == 2
    assert all(isinstance(r.raw, list) for r in todo_results)
    assert all(isinstance(item, TodoItem) for item in todo_results[0].raw)


@pytest.mark.asyncio
async def test_system_prompt_carries_todo_instructions() -> None:
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="t", model=provider, plugins=[Todo()])
    await Runner.run(agent, "go")
    system = provider.calls[0][0]
    assert system.role == "system"
    assert "todo_write" in (system.content or "")


@pytest.mark.asyncio
async def test_todos_carry_across_handoff() -> None:
    # Agent A writes todos, then hands off to B (also has the plugin). B's
    # injector rehydrates A's todos from the transcript, so B's model call sees
    # the reminder even though B's store started empty.
    args = {
        "todos": [
            {
                "content": "shared task",
                "status": "in_progress",
                "active_form": "Doing shared task",
            }
        ]
    }
    b = Agent(name="B", model=ScriptedProvider([text("done by B")]), plugins=[Todo()])
    a = Agent(
        name="A",
        model=ScriptedProvider(
            [
                call("todo_write", args, call_id="c1"),
                call("transfer_to_b", {"reason": "continue"}, call_id="h1"),
            ]
        ),
        plugins=[Todo()],
        handoffs=[b],
    )
    await Runner.run(a, "go")

    b_calls = b.model.calls  # type: ignore[attr-defined]
    assert b_calls, "B never ran"
    assert any(
        "Doing shared task" in (m.content or "") for turn in b_calls for m in turn
    )


@pytest.mark.asyncio
async def test_inject_false_omits_reminder() -> None:
    args = {"todos": [{"content": "x"}]}
    provider = ScriptedProvider([call("todo_write", args, call_id="c1"), text("done")])
    agent = Agent(name="t", model=provider, plugins=[Todo(inject=False)])
    await Runner.run(agent, "go")
    assert not any(
        m.role == "user" and "system-reminder" in (m.content or "")
        for turn in provider.calls
        for m in turn
    )
