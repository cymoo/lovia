# Plugins

A reusable capability may need Tools, instructions, per-turn reminders, hooks,
and cleanup. A **plugin** packages them together. Skills, MCP, Todo, and Memory
all use this extension mechanism.

```python
from lovia import Agent, Memory, Skills, Todo

agent = Agent(
    name="builder",
    model="<model>",
    plugins=[Todo(), Skills("./skills"), Memory("./.lovia/memory")],
)
```

If a capability is one independent function, an ordinary `@tool` is usually
clearer. Reach for a Plugin when the capability must also carry instructions,
state, per-turn reminders, or cleanup.

## The contract

A plugin is any object with a `name` and an async `setup(ctx)` returning a
`PluginInstance`:

```python
class Plugin(Protocol):
    name: str
    async def setup(self, ctx: RunContext) -> PluginInstance: ...
```

The runner awaits `setup()` **once per run** — and once per agent a
[handoff](multi-agent.md) reaches — then merges the returned contributions
into the loop's fixed slots.

`ctx` is the run's live `RunContext`, the same handle tools and hooks
receive, so a plugin can shape its contributions per session or user. At
setup time `session_id`, `run_id`, and `context` are final, and `agent` /
`workspace` are the agent being activated; `entries` is empty at run start,
so derive nothing from it here.

| `PluginInstance` field | Effect |
| --- | --- |
| `tools` | merged into the agent's tool set (one namespace — clashes raise like any other tool source) |
| `instructions` | static text appended to the system prompt, rendered once at run start |
| `view_injectors` | called **every turn**; their entries are appended to that turn's model view only — never persisted |
| `hooks` | an `AgentHooks` receiving every run event, dispatched alongside the agent's own |
| `input_guardrails` / `output_guardrails` | merged with the agent's own at the loop's existing checkpoints |
| `aclose` | coroutine awaited when the run ends (LIFO across plugins, best effort) |

Plugin contributions merge into fixed extension points; the Plugin does not
take over control flow. The loop still executes abort, retry, and handoff. A
Plugin guardrail can stop a Run only at the same checkpoint as an Agent
guardrail.

`name` is the plugin's identity: unique per agent (validated before any
`setup()` runs) and stable.

## View injectors: the per-turn seam

The novel slot. A `ViewInjector` is called with the live `RunContext` each
turn and returns transcript entries to append to **that turn's model
view**:

```python
def inject(ctx: RunContext) -> list[TranscriptEntry] | None:
    if not store.items:
        return None
    return [InputEntry(role="user", content=f"<system-reminder>\n{render(store.items)}\n</system-reminder>")]
```

Because injected entries never enter the transcript or the session, they
don't accumulate as turns grow, they don't bust the provider's cached
prompt prefix, and a resumed run doesn't replay them — an injector is
expected to regenerate its content each turn (reminders, clocks, todo
lists). Injectors are **fail-open**: one that raises is logged and skipped,
never aborting the run. Keep them small — they are appended *after* the
[context policy](context.md) has shaped the view.

## State scoping: the one design decision

Where a plugin keeps state decides its behavior under concurrency:

- **Per-run state** is built *inside* `setup()` and closed over — every run
  gets a fresh copy, concurrency-safe by construction. The todo list below
  works this way.
- **Cross-run state** (a database, an index, a glossary) lives *on the
  plugin object*, passed in at construction. It is shared by every run —
  possibly concurrently — so it must be safe for concurrent use, and the
  plugin never closes it: its lifecycle belongs to whoever created it.
  [Memory](memory.md) works this way.

## Worked example: a cross-session glossary

The full shape of a stateful plugin — a shared backend, one tool, prompt
text — in one screen:

```python
from dataclasses import dataclass
from typing import Protocol

from lovia import Agent, PluginInstance, RunContext, tool


class Glossary(Protocol):
    """Your shared backend — a DB, a file, an in-memory dict."""

    async def define(self, term: str, meaning: str) -> None: ...
    async def lookup(self, term: str) -> str | None: ...


@dataclass
class GlossaryPlugin:
    """Cross-session glossary the agent can write to and read back."""

    store: Glossary          # long-lived, shared by every run
    name: str = "glossary"

    async def setup(self, ctx: RunContext) -> PluginInstance:
        store = self.store

        @tool
        async def define(term: str, meaning: str) -> str:
            """Record what a domain term means, for this and later sessions."""
            await store.define(term, meaning)
            return f"Noted: {term}."

        @tool
        async def lookup(term: str) -> str:
            """Look up a previously defined domain term."""
            return await store.lookup(term) or f"No definition for {term!r}."

        return PluginInstance(
            tools=[define, lookup],
            instructions="Use `define` to record domain terms the user explains, "
            "and `lookup` before asking the user to re-explain one.",
        )


agent = Agent(name="assistant", model="<model>", plugins=[GlossaryPlugin(MyGlossary())])
```

A plugin that opens a resource in `setup()` (an MCP connection, an HTTP
client) returns it via `aclose`:

```python
async def setup(self, ctx: RunContext) -> PluginInstance:
    conn = await open_connection(self.url)
    return PluginInstance(tools=tools_from(conn), aclose=conn.close)
```

## Worked example: a loop guard

A plugin needn't add tools at all. This one pairs a hook (observe) with a view
injector (nudge): when the model keeps retrying a tool call that fails the same
way, it says so — once.

```python
import json
from dataclasses import dataclass
from typing import Any

from lovia import Agent, AgentHooks, PluginInstance, RunContext, events
from lovia.transcript import InputEntry, TranscriptEntry


@dataclass
class LoopGuard:
    """Nudge the model when it keeps retrying the same failing tool call."""

    threshold: int = 3
    name: str = "loop_guard"

    async def setup(self, ctx: RunContext[Any]) -> PluginInstance:
        # Per-run state, closed over by the hook and the injector below.
        last: str | None = None  # signature of the latest failed call
        streak = 0
        pending: str | None = None  # a reminder owed to the next turn

        hooks = AgentHooks()

        @hooks.on(events.ToolCallCompleted)
        def track(ev: events.ToolCallCompleted, ctx: RunContext[Any]) -> None:
            nonlocal last, streak, pending
            if not ev.is_error:  # any success ends the streak
                last, streak, pending = None, 0, None
                return
            try:
                args = json.dumps(json.loads(ev.call.arguments), sort_keys=True)
            except json.JSONDecodeError:
                args = ev.call.arguments
            sig = f"{ev.call.name}:{args}"
            streak = streak + 1 if sig == last else 1
            last = sig
            if streak == self.threshold:  # == not >=: once per streak
                pending = (
                    f"You called `{ev.call.name}` with identical arguments "
                    f"{streak} times and it failed each time. Change approach "
                    "instead of retrying the same call."
                )

        def remind(ctx: RunContext[Any]) -> list[TranscriptEntry] | None:
            nonlocal pending
            if pending is None:
                return None
            text, pending = pending, None
            content = f"<system-reminder>\n{text}\n</system-reminder>"
            return [InputEntry(role="user", content=content)]

        return PluginInstance(hooks=hooks, view_injectors=[remind])


agent = Agent(name="builder", model="<model>", plugins=[LoopGuard()])
```

- **Only tool errors count** (`ToolCallCompleted.is_error`). A shell command
  that exits non-zero is not an error in lovia — the exit code is just part of
  the output — so polling, or rerunning tests after an edit, never trips it.
- **The signature is the tool name plus canonical JSON** of the arguments, so
  key order and whitespace don't matter; any successful call resets the streak.
- **One reminder per streak**, on the turn after the `threshold`-th identical
  failure. It is a view entry, never written to the transcript or session, so
  the cached prompt prefix stays stable.
- **No hard stop.** A model that ignores the nudge still runs into
  `max_turns` / [`RunBudget`](budgets.md) — those remain the backstop.

## The built-in plugins

| Plugin | One line | Guide |
| --- | --- | --- |
| `Todo()` | externalized checklist, re-shown every turn | [Todo](todo.md) |
| `Skills(...)` | instruction bundles with progressive disclosure | [Skills](skills.md) |
| `MCP(...)` | tools from Model Context Protocol servers | [MCP](mcp.md) |
| `Memory(...)` | long-term, cross-session memory | [Memory](memory.md) |

## Sharp edges

- **Run state on the plugin object is a concurrency bug.** Two concurrent
  runs of one agent share the plugin instance; anything mutable that isn't
  built inside `setup()` is shared mutable state.
- **`setup()` runs per agent, per run — including handoff targets.** A
  plugin attached to both sides of a handoff activates twice in one run;
  design `setup()` to be cheap and idempotent in effect.
- **Instructions are static per run.** `PluginInstance.instructions` is
  rendered once at run start; content that must vary turn-by-turn belongs in
  a view injector instead.
- **Injected view entries are invisible to persistence** — by design. If a
  reminder must be auditable later, make it a tool result instead.

## See also

- [Todo](todo.md) · [Skills](skills.md) · [MCP](mcp.md) · [Memory](memory.md) — the built-ins
  in depth
- [Context management](context.md) — how views are assembled around injectors
- Examples: [`21_todos.py`](../../examples/21_todos.py),
  [`25_custom_plugin.py`](../../examples/25_custom_plugin.py)
