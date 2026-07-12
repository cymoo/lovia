# Plugins

Reusable capabilities usually need several things at once — a tool, prompt
text explaining it, a per-turn reminder, maybe a hook and a teardown. Most
frameworks make you wire each into a separate registry. A lovia **plugin**
is one object that contributes all of them, and it is the framework's *only*
extension axis: Skills, MCP, Todo, and Memory are all built on exactly this
seam.

```python
from lovia import Agent, Memory, Skills, Todo

agent = Agent(
    name="builder",
    model="<model>",
    plugins=[Todo(), Skills("./skills"), Memory("./.lovia/memory")],
)
```

## The contract

A plugin is any object with a `name` and an async `setup()` returning a
`PluginInstance`:

```python
class Plugin(Protocol):
    name: str
    async def setup(self) -> PluginInstance: ...
```

The runner awaits `setup()` **once per run** — and once per agent a
[handoff](multi-agent.md) reaches — then merges the returned contributions
into the loop's fixed slots:

| `PluginInstance` field | Effect |
| --- | --- |
| `tools` | merged into the agent's tool set (one namespace — clashes raise like any other tool source) |
| `instructions` | static text appended to the system prompt, rendered once at run start |
| `view_injectors` | called **every turn**; their entries are appended to that turn's model view only — never persisted |
| `hooks` | an `AgentHooks` receiving every run event, dispatched alongside the agent's own |
| `input_guardrails` / `output_guardrails` | merged with the agent's own at the loop's existing checkpoints |
| `aclose` | coroutine awaited when the run ends (LIFO across plugins, best effort) |

Plugins are **purely additive**: they never drive control flow. The loop
keeps the abort, the retry, and the handoff — a plugin's guardrail can trip
a run only through the same checkpoint the agent's own guardrails use.

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

from lovia import Agent, PluginInstance, tool


class Glossary(Protocol):
    """Your shared backend — a DB, a file, an in-memory dict."""

    async def define(self, term: str, meaning: str) -> None: ...
    async def lookup(self, term: str) -> str | None: ...


@dataclass
class GlossaryPlugin:
    """Cross-session glossary the agent can write to and read back."""

    store: Glossary          # long-lived, shared by every run
    name: str = "glossary"

    async def setup(self) -> PluginInstance:
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
async def setup(self) -> PluginInstance:
    conn = await open_connection(self.url)
    return PluginInstance(tools=tools_from(conn), aclose=conn.close)
```

## The built-in plugins

| Plugin | One line | Guide |
| --- | --- | --- |
| `Todo()` | externalized checklist, re-shown every turn | [Todo](todo.md) |
| `Skills(...)` | instruction bundles with progressive disclosure | [Skills](skills.md) |
| `MCP(...)` | tools from Model Context Protocol servers | [MCP](mcp.md) |
| `Memory(...)` | long-term, cross-session memory | [Memory](memory.md) |

`Todo` has its own guide because its model-facing workflow, recovery behavior,
and observation API are useful without writing a custom plugin. See
[Todo](todo.md).

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
