"""Tests for the todo plugin: store semantics, rendering, the staleness reminder."""

from __future__ import annotations

import json

import pytest

from lovia import Agent, Runner, Todo, tool
from lovia.exceptions import UserError
from lovia.plugins.todo import (
    TodoItem,
    TodoList,
    _calls_since_write,
    render_todos,
    todos_from_entries,
)
from lovia.stores import InMemorySession
from lovia.transcript import (
    AssistantTextEntry,
    InputEntry,
    ReasoningEntry,
    ToolCallEntry,
    ToolResultEntry,
)

from ..scripted_provider import ScriptedProvider, call, text


def _inputs(*items: dict) -> list[TodoItem]:
    return [TodoItem.model_validate(i) for i in items]


def test_replace_is_full_overwrite() -> None:
    store = TodoList()
    store.replace(_inputs({"content": "a"}, {"content": "b"}))
    assert [t.content for t in store.items] == ["a", "b"]
    store.replace(_inputs({"content": "c"}))
    assert [t.content for t in store.items] == ["c"]


def test_replace_copies_inputs_and_returns_detached_list() -> None:
    mine = _inputs(
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"},
    )
    store = TodoList()
    returned = store.replace(mine)
    # Normalization demoted the store's copy, not the caller's objects.
    assert [t.status for t in mine] == ["in_progress", "in_progress"]
    assert [t.status for t in store.items] == ["in_progress", "pending"]
    # Mutating the returned list must not corrupt the store.
    returned.append(TodoItem(content="sneaky"))
    assert [t.content for t in store.items] == ["a", "b"]


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


def _has_reminder(turn) -> bool:
    return any(
        m.role == "user" and "system-reminder" in (m.content or "") for m in turn
    )


@tool(name="work")
async def _work() -> str:
    return "ok"


_OPEN = {
    "todos": [
        {"content": "Plan", "status": "completed"},
        {"content": "Build", "status": "in_progress", "active_form": "Building"},
    ]
}


@pytest.mark.asyncio
async def test_reminder_waits_until_the_list_goes_stale() -> None:
    """The write's own result shows the list; the reminder comes back only
    after ``remind_every`` calls without a write, then every as many again."""
    provider = ScriptedProvider(
        [
            call("todo_write", _OPEN, call_id="c1"),
            *[call("work", {}, call_id=f"w{i}") for i in range(5)],
            text("done"),
        ]
    )
    agent = Agent(
        name="t", model=provider, tools=[_work], plugins=[Todo(remind_every=2)]
    )
    await Runner.run(agent, "go")

    # Calls since the write: -, 0, 1, 2, 3, 4, 5.
    assert [_has_reminder(c) for c in provider.calls] == [
        False, False, False, True, False, True, False,
    ]  # fmt: skip
    assert any("Building" in (m.content or "") for m in provider.calls[3])


@pytest.mark.asyncio
async def test_a_write_restarts_the_count() -> None:
    provider = ScriptedProvider(
        [
            call("todo_write", _OPEN, call_id="c1"),
            call("work", {}, call_id="w1"),
            call("todo_write", _OPEN, call_id="c2"),
            call("work", {}, call_id="w2"),
            text("done"),
        ]
    )
    agent = Agent(
        name="t", model=provider, tools=[_work], plugins=[Todo(remind_every=2)]
    )
    await Runner.run(agent, "go")
    assert not any(_has_reminder(c) for c in provider.calls)


@pytest.mark.asyncio
async def test_a_finished_list_is_not_re_shown() -> None:
    done = {"todos": [{"content": "Plan", "status": "completed"}]}
    provider = ScriptedProvider(
        [
            call("todo_write", done, call_id="c1"),
            *[call("work", {}, call_id=f"w{i}") for i in range(3)],
            text("done"),
        ]
    )
    agent = Agent(
        name="t", model=provider, tools=[_work], plugins=[Todo(remind_every=1)]
    )
    await Runner.run(agent, "go")
    assert not any(_has_reminder(c) for c in provider.calls)


@pytest.mark.asyncio
async def test_staleness_spans_runs() -> None:
    """Plain chat, one call per run: the count comes from the transcript, so
    the runs since the write add up."""
    provider = ScriptedProvider(
        [call("todo_write", _OPEN, call_id="c1"), text("a"), text("b"), text("c")]
    )
    agent = Agent(name="t", model=provider, plugins=[Todo(remind_every=2)])
    session = InMemorySession()
    for prompt in ("one", "two", "three"):
        await Runner.run(agent, prompt, session=session, session_id="s")
    # Calls since the write: -, 0 | 1 | 2.
    assert [_has_reminder(c) for c in provider.calls] == [False, False, False, True]


def test_calls_since_write_counts_model_calls_not_entries() -> None:
    write = ToolCallEntry(call_id="c", name="todo_write", arguments=json.dumps(_OPEN))
    bad = ToolCallEntry(call_id="b", name="todo_write", arguments='{"todos": 1}')
    other = ToolCallEntry(call_id="o", name="work", arguments="{}")

    def res(call_id: str) -> ToolResultEntry:
        return ToolResultEntry(call_id=call_id, output="ok")

    assert (
        _calls_since_write([InputEntry(role="user", content="hi")], "todo_write")
        is None
    )
    # The write shares its call with another tool call: that call is not counted.
    assert _calls_since_write([write, other, res("c"), res("o")], "todo_write") == 0
    # One later call with reasoning, text and two parallel tool calls.
    later = [
        ReasoningEntry(content="r"),
        AssistantTextEntry(content="t"),
        other,
        other,
        res("o"),
        res("o"),
    ]
    assert _calls_since_write([write, res("c"), *later], "todo_write") == 1
    # A malformed newer write changed nothing: count from the valid one.
    entries = [write, res("c"), bad, res("b"), AssistantTextEntry(content="t")]
    assert _calls_since_write(entries, "todo_write") == 2


@pytest.mark.parametrize("every", [0, -1, 1.5, True, "8"])
def test_remind_every_must_be_a_positive_int(every) -> None:
    with pytest.raises(UserError):
        Todo(remind_every=every)


@pytest.mark.asyncio
async def test_end_to_end_audit_trail() -> None:
    first = {
        "todos": [
            {"content": "Plan", "status": "in_progress", "active_form": "Planning"},
            {"content": "Build"},
        ]
    }
    provider = ScriptedProvider(
        [
            call("todo_write", first, call_id="c1"),
            call("todo_write", _OPEN, call_id="c2"),
            text("all done"),
        ]
    )
    agent = Agent(name="t", model=provider, plugins=[Todo(remind_every=1)])
    result = await Runner.run(agent, "do the thing")
    assert not any(_has_reminder(c) for c in provider.calls[:2])

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
    # the reminder even though B's store started empty. (``remind_every=1``:
    # B's first call is one call after A's write.)
    args = {
        "todos": [
            {
                "content": "shared task",
                "status": "in_progress",
                "active_form": "Doing shared task",
            }
        ]
    }
    b = Agent(
        name="B",
        model=ScriptedProvider([text("done by B")]),
        plugins=[Todo(remind_every=1)],
    )
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
        m.role == "user"
        and "system-reminder" in (m.content or "")
        and "Doing shared task" in (m.content or "")
        for turn in b_calls
        for m in turn
    )


@pytest.mark.asyncio
async def test_remind_every_none_omits_reminder() -> None:
    args = {"todos": [{"content": "x"}]}
    provider = ScriptedProvider(
        [call("todo_write", args, call_id="c1"), call("work", {}), text("done")]
    )
    agent = Agent(
        name="t", model=provider, tools=[_work], plugins=[Todo(remind_every=None)]
    )
    await Runner.run(agent, "go")
    assert not any(
        m.role == "user" and "system-reminder" in (m.content or "")
        for turn in provider.calls
        for m in turn
    )
