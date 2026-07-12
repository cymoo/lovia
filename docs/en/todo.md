# Todo

`Todo` gives an Agent an externalized checklist for multi-step work. The model
updates it through a typed Tool, and a transient reminder re-shows the current
plan every Turn without growing the Transcript.

```python
from lovia import Agent, Runner, Todo

agent = Agent(
    name="builder",
    instructions="Complete multi-step work carefully.",
    model="<model>",
    plugins=[Todo()],
)

result = await Runner.run(
    agent,
    "Implement a small REST API with tests and documentation.",
)
```

## How it works

The plugin contributes two pieces:

- `todo_write`, a full-replacement Tool. Every call contains the complete
  list, so the latest valid result is authoritative.
- A View injector that renders the current list as a `<system-reminder>` on
  every Turn. The reminder is transient: it does not enter the Transcript,
  invalidate the stable prompt prefix, or accumulate over time.

Each `TodoItem` contains `content`, a status (`pending`, `in_progress`, or
`completed`), and an optional `active_form`. At most one item remains
`in_progress`; extra items are demoted rather than rejecting the whole update.

## Configuration

```python
Todo(
    tool_name="todo_write",
    inject=True,
    instructions=None,  # None removes the built-in usage guidance
)
```

Set `inject=False` to keep the Tool without the per-Turn reminder. Use a custom
`tool_name` only when another Tool already owns `todo_write`.

## Recovery and observation

The store itself is Run-scoped, but the list survives interruption and
Handoff: on activation, the plugin reconstructs state from the newest valid
`todo_write` call in the Transcript.

Host applications can observe `ToolCallCompleted` events where
`event.call.name == "todo_write"`; `event.result` is the structured
`list[TodoItem]`. For stored conversations, call
`lovia.plugins.todos_from_entries(entries)`. The bundled Web UI uses this same
function.

## Sharp edges

- Todo is a planning aid, not a scheduler or durable job queue. Use
  [scheduling](web-server.md#scheduling) for future work.
- Transient reminders are intentionally absent from Sessions and Checkpoints;
  audit the `todo_write` Tool calls instead.
- A vague task may cause noisy plan churn. Instructions should reserve Todo for
  genuinely multi-step work.

## See also

- [Plugins](plugins.md) — lifecycle and View injectors
- [Streaming](streaming.md) — observing Tool events
- Example: [`21_todos.py`](../../examples/21_todos.py)
