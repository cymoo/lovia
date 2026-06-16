# lovia

[中文文档](./README-zh.md)

lovia is a lightweight Python agent framework for people who want an agent
loop, not a platform. It gives you the useful primitives: tools, streaming,
structured output, sessions, handoff, guardrails, approvals, workspaces,
skills, plugins, MCP, and a small web UI, while keeping the core easy to read
and easy to replace.

```bash
pip install lovia
```

```python
import asyncio
from lovia import Agent, Runner, tool


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


async def main() -> None:
    agent = Agent(
        name="calculator",
        instructions="Use tools when useful. Answer briefly.",
        model="deepseek-v4-pro",
        tools=[add],
    )
    result = await Runner.run(agent, "What is 21 + 21?")
    print(result.output)


asyncio.run(main())
```

Set `OPENAI_API_KEY` for the official OpenAI endpoint, or set
`OPENAI_BASE_URL` to use any OpenAI-compatible service. Anthropic is built in
too: `model="anthropic:claude-4-5-sonnet"`.

## Why lovia

The agent ecosystem is full of heavy abstractions. lovia makes a different
bet: the framework should be small enough to understand, but serious enough to
ship.

- **Small mental model.** An `Agent` describes behavior, `Runner` executes it,
  and `@tool` exposes Python functions. Most of the framework follows from
  those three ideas.
- **Provider-neutral by design.** Built-in adapters speak OpenAI Chat
  Completions and Anthropic Messages directly over `httpx`; custom providers
  implement a small `Protocol`.
- **Python-native extension.** Agents are dataclasses; providers, sessions,
  plugins, skills, and workspaces are protocol-shaped. You plug things
  in; you do not subclass a framework universe.
- **Minimal by default.** The base install stays focused. Search, MCP, web UI,
  example niceties, and orchestration integrations live behind extras.
- **Production primitives without ceremony.** Approvals, guardrails, retries,
  budgets, cancellation, checkpoint/resume, context compaction, lifecycle
  hooks, and scoped workspace tools are available when you need them.

## Philosophy

lovia optimizes for four things, in this order:

1. **Concise.** A feature should fit in your head. The public surface should
   be obvious, and internals should be readable when you need to debug.
2. **Lightweight.** The core should import quickly, install cleanly, and avoid
   dragging in infrastructure you did not ask for.
3. **Extensible.** Real applications need their own providers, storage,
   policies, tools, and UI. lovia gives you seams instead of lock-in.
4. **General-purpose.** The built-ins are practical, but not magical. They are
   examples of the same extension points you can use yourself.

## The Core API

### Agent

`Agent` is declarative runtime configuration. It has no conversation state, so
it is safe to reuse across requests.

```python
from lovia import Agent

agent = Agent(
    name="writer",
    instructions="Write concrete, concise answers.",
    model="deepseek-v4-pro",
)
```

Dynamic prompt fragments can depend on per-run context:

```python
@agent.system_prompt
async def user_tier(ctx) -> str:
    return f"User tier: {ctx.context['tier']}"
```

Create request-specific variants with `clone()`:

```python
strict = agent.clone(instructions="Answer with citations only.")
```

### Runner

```python
from lovia import Runner

result = await Runner.run(agent, "Draft a release note.")
print(result.output)
```

The handle returned by `stream()` is both async-iterable and awaitable:

```python
from lovia import events

handle = Runner.stream(agent, "Explain context windows in one paragraph.")

async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

Scripts can use the sync wrapper:

```python
result = Runner.run_sync(agent, "Summarize this file.")
```

### Tools

Any typed Python callable can become a tool. lovia derives the tool schema from
type hints, docstrings, `Annotated`, and Pydantic `Field` metadata.

```python
from typing import Annotated
from pydantic import Field
from lovia import tool


@tool
async def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"{order_id}: shipped"


@tool(strict=True)
def search_docs(
    query: Annotated[str, "Search terms"],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """Search internal documentation."""
    return []
```

Sync tools run in a worker thread. Async tools are awaited directly.

## Structured Output

Pass a Pydantic model, dataclass, `TypedDict`, or supported Python type and the
final result is validated for you. If parsing fails, lovia asks the model once
to repair the response by default.

```python
from pydantic import BaseModel
from lovia import Agent, Runner


class Brief(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(
    name="summarizer",
    model="deepseek-v4-pro",
    output_type=Brief,
)

result = await Runner.run(agent, "Summarize lovia for a Python developer.")
print(result.output.title)
```

You can override output type per call:

```python
result = await Runner.run(agent, "Return a launch checklist.", output_type=list[str])
```

## Provider Choice

Use a model string, a provider instance, or a fallback chain:

```python
from lovia import Agent, ModelSettings

agent = Agent(
    name="assistant",
    model=[
        "anthropic:claude-4-5-sonnet",
        "deepseek-v4-pro",
    ],
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
```

Custom providers implement the `Provider` protocol and can be registered with
the `lovia.providers` entry-point group.

## Multi-Agent Workflows

### Handoff

Handoff lets one agent transfer control to a specialist. The transcript follows
the handoff, optionally filtered.

```python
from lovia import Agent, Handoff, Runner

billing = Agent(name="billing", instructions="Handle billing issues.", model="deepseek-v4-pro")
support = Agent(name="support", instructions="Handle technical issues.", model="deepseek-v4-pro")

triage = Agent(
    name="triage",
    instructions="Route the user to the right specialist.",
    model="deepseek-v4-pro",
    handoffs=[billing, support],
)

result = await Runner.run(triage, "I was charged twice.")
```

### Agent As Tool

Use an agent as a delegated subroutine:

```python
summarizer = Agent(
    name="summarizer",
    instructions="Summarize text in five bullets.",
    model="deepseek-v4-pro",
)

manager = Agent(
    name="manager",
    instructions="Delegate summarization when useful.",
    model="deepseek-v4-pro",
    tools=[summarizer.as_tool(description="Summarize a passage.")],
)
```

The sub-agent runs in its own loop and returns its final output as the tool
result.

## Human Control

### Tool Approval

Gate sensitive actions with `needs_approval=True`.

```python
from lovia import tool


@tool(needs_approval=True)
async def refund(order_id: str, amount_cents: int) -> str:
    """Issue a refund."""
    return "refunded"
```

In streaming mode, resolve approvals from your UI:

```python
from lovia import events

handle = Runner.stream(agent, "Refund order A123.")

async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()          # or ev.reject()
```

For server-side policy:

```python
agent = agent.clone(
    approval_handler=lambda call, ctx: "ask" if call.name == "refund" else "allow"
)
```

### Ask A Human

`ask_human` lets the model request operator input through your application.

```python
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()

agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    tools=[ask_human(channel)],
)

# Somewhere in your UI/event loop:
for question in channel.pending:
    channel.answer(question.id, "Use option A.")
```

## Sessions and Checkpoints

Sessions persist conversation transcript across calls:

```python
from lovia.stores import SQLiteSession

session = SQLiteSession("chat.db")

await Runner.run(agent, "My project is called Atlas.", session=session, session_id="u1")
result = await Runner.run(agent, "What is my project called?", session=session, session_id="u1")
```

Checkpoints are for crash recovery and idempotent long runs:

```python
from lovia import CheckpointOptions
from lovia.stores import SQLiteCheckpointer

checkpoint = SQLiteCheckpointer("runs.db")

result = await Runner.run(
    agent,
    "Migrate the report format.",
    checkpoint=CheckpointOptions(checkpoint, "report-migration-42"),
)
```

Long-term memory across sessions is not a core concept — there is no `Memory`
type. Add it as a plugin over your own store (see the `MemoryPlugin` example
under Plugins below).

## Context Management

Long conversations use `Compaction` by default. It is view-only: the full
transcript stays in the session/checkpoint, while the per-model-call view can
offload huge tool results, clear older tool results, and summarize old history
under token pressure.

```python
from lovia import Compaction, Runner

policy = Compaction(
    context_window=200_000,
    compact_at=0.75,
    compact_to=0.50,
)

result = await Runner.run(agent, "Continue.", context_policy=policy)
```

Add `recall_tool_result` when you want the model to recover a compacted tool
result without re-running the tool:

```python
from lovia.tools import recall_tool_result

agent = agent.clone(tools=[*agent.tools, recall_tool_result])
```

Use `NoopContextPolicy()` to disable automatic compaction.

## Guardrails, Reliability, Hooks

Input and output guardrails are async callables. Raise `GuardrailTripped` or
return a truthy violation message to stop the run.

```python
from lovia.exceptions import GuardrailTripped


async def no_email_addresses(messages, ctx):
    if any("@" in str(m.content) for m in messages):
        raise GuardrailTripped("Email addresses are not allowed.")


async def must_cite(output, ctx):
    if "source:" not in output.lower():
        return "Missing source citation."


agent = Agent(
    name="researcher",
    model="deepseek-v4-pro",
    input_guardrails=[no_email_addresses],
    output_guardrails=[must_cite],
)
```

Budgets, cancellation, and retry policies are explicit:

```python
from lovia import RetryPolicy, RunBudget

result = await Runner.run(
    agent,
    "Analyze these logs.",
    budget=RunBudget(max_tool_calls=20, max_seconds=60),
    retry=RetryPolicy(max_attempts=3),
)
```

Lifecycle hooks receive the same typed events used by streaming:

```python
from lovia import events
from lovia.hooks import AgentHooks

hooks = AgentHooks()


@hooks.on(events.ToolCallStarted)
async def log_tool(ev):
    print(ev.call.name, ev.call.arguments)


agent = agent.clone(hooks=hooks)
```

## Built-In Tools

Nothing is imported into your agent automatically. Pick the tools you want.

```python
from lovia.tools.http import http_fetch
from lovia.tools.time import now
from lovia.tools.search import duckduckgo_search_tool

agent = Agent(
    name="researcher",
    model="deepseek-v4-pro",
    tools=[http_fetch, now, duckduckgo_search_tool()],
)
```

Install DuckDuckGo search support with:

```bash
pip install "lovia[ddg]"
```

Custom search is just a `WebSearch` implementation passed to `web_search()`.

## Plugins

A **plugin** is lovia's one extension axis for bundling a feature. A single
object contributes any mix of: `tools`, system-prompt `instructions`, per-turn
`view_injectors` (transient reminders, never written to the transcript), event
`hooks`, and `input_guardrails` / `output_guardrails`. The runner activates each
plugin **once per run** (and once per agent on a handoff) by awaiting its async
`setup()`, and releases anything it opened via `aclose()` when the run ends.
Plugins are purely additive — they never drive control flow; the loop keeps the
abort, retry, and handoff. Skills, MCP, and the todo list below are all built-in
plugins.

### Todo lists

The built-in todo plugin gives the model a checklist tool and re-shows the
current list every turn, without bloating the persisted transcript:

```python
from lovia import Agent, Runner, todos

agent = Agent(
    name="builder",
    instructions="Complete multi-step work carefully.",
    model="deepseek-v4-pro",
    plugins=[todos()],
)

await Runner.run(agent, "Implement a small REST API with tests and docs.")
```

### Skills

Skills are reusable instruction bundles following the Agent Skills
specification. lovia exposes skill metadata up front, then lets the model load
full instructions and referenced files only when needed.

```python
from lovia import Agent, skills

agent = Agent(
    name="support",
    instructions="Help customers using the right policy.",
    model="deepseek-v4-pro",
    plugins=[skills("./skills")],
)
```

A skill directory holds `SKILL.md` with YAML frontmatter, plus optional
`references/`, `scripts/`, and `assets/` files. Pass several directories, or
scope the catalog with a filter:

```python
plugins=[skills("./skills", "./team-skills")]
plugins=[skills("./skills", filter=lambda meta: "internal" not in meta.extra.get("tags", []))]
```

For a custom backend, pass a `SkillSource` (or a pre-built `Skills`) instead of
paths.

### MCP

[Model Context Protocol](https://modelcontextprotocol.io) servers expose their
tools to the agent. Install the optional dependency:

```bash
pip install "lovia[mcp]"
```

```python
from lovia import Agent
from lovia.plugins.mcp import MCPServerStdio, mcp

agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    plugins=[
        mcp(MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"]))
    ],
)
```

By default each run opens and closes the server. To reuse one connection across
runs, open a session and pass the live connection instead:

```python
server = MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])

async with server.session() as conn:
    agent = Agent(name="assistant", model="deepseek-v4-pro", plugins=[mcp(conn)])
    await Runner.run(agent, "Fetch https://example.com and summarize it.")
```

`mcp()` takes several servers — `mcp(a, b)` — and `MCPServer.name` prefixes a
server's tools (`web__fetch`) to keep names unique.

### Writing a plugin

A plugin is any object with a `name` and an `async setup()` that returns a
`PluginInstance`. State that should be **fresh per run** is built inside `setup`
(like the todo list above); state that should **persist across runs and
sessions** is held on the plugin and passed in at construction. Here is a
long-term memory plugin — it wraps a backend you supply, created once and shared
by every run, so the agent can recall a fact from one conversation in the next:

```python
from dataclasses import dataclass
from typing import Protocol

from lovia import Agent, PluginInstance, tool


class MemoryStore(Protocol):
    """Your long-term backend — implement it over a vector DB, SQLite, etc."""

    async def add(self, fact: str) -> None: ...
    async def search(self, query: str, k: int) -> list[str]: ...


@dataclass
class MemoryPlugin:
    """Cross-session long-term memory the agent can write to and search."""

    store: MemoryStore  # long-lived, shared by every run — not rebuilt per run
    name: str = "memory"

    async def setup(self) -> PluginInstance:
        store = self.store

        @tool
        async def remember(fact: str) -> str:
            """Save a durable fact to recall in any later session."""
            await store.add(fact)
            return "Saved to long-term memory."

        @tool
        async def recall(query: str) -> str:
            """Search long-term memory for facts relevant to the query."""
            hits = await store.search(query, k=5)
            return "\n".join(f"- {h}" for h in hits) or "(nothing relevant)"

        return PluginInstance(
            tools=[remember, recall],
            instructions=(
                "You have long-term memory that persists across sessions. Call "
                "`recall` to check it before answering, and `remember` to store "
                "durable facts or preferences the user shares."
            ),
        )


store = MyVectorStore()  # your MemoryStore: just async add() and search()
agent = Agent(name="assistant", model="deepseek-v4-pro", plugins=[MemoryPlugin(store)])
```

Because that backend is shared across (possibly concurrent) runs it must be safe
for concurrent use, and the plugin never closes it — its lifecycle belongs to
whoever created it. (Contrast the todo plugin, whose store is rebuilt inside
`setup` for each run.)

`PluginInstance` carries any subset of these contributions:

| Field | Effect |
| --- | --- |
| `tools` | merged into the agent's tool set |
| `instructions` | appended to the system prompt |
| `view_injectors` | entries appended to the model's view each turn — never persisted |
| `hooks` | an `AgentHooks` that observes run events (metrics, audit, …) |
| `input_guardrails` / `output_guardrails` | run at the loop's checkpoints, with the agent's own; the loop keeps the abort |
| `aclose` | coroutine awaited at run end to release resources opened in `setup` |

## Workspace Agents

`Workspace` adds file and shell tools scoped to a root directory and permission
policy.

```python
from lovia import Agent
from lovia.workspace import CommandRule, Workspace

agent = Agent(
    name="coder",
    instructions="Make small, targeted code changes.",
    model="deepseek-v4-pro",
    workspace=Workspace.local(
        ".",
        mode="coding",
        denied_paths=(".env*",),
        command_rules=(
            CommandRule("pytest", "allow"),
            CommandRule("rm -rf", "deny"),
        ),
    ),
)
```

Modes:

| Mode | Tools |
| --- | --- |
| `readonly` | `read_file`, `list_files`, `grep_files` |
| `coding` | read tools plus `write_file`, `edit_file`, `shell` with approval by default |
| `trusted` | coding tools with shell allowed by default |

Workspace paths are root-relative; absolute paths, `..` escapes, and symlink
escapes are rejected. The local shell still runs as the host user, so use
containerized or remote workspace backends when you need hard isolation.

## Web UI

The optional web layer is a small FastAPI app with SSE streaming, sessions,
markdown rendering, and approval routes.

```bash
pip install "lovia[web]"
```

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

## Install Extras

| Need | Install |
| --- | --- |
| Core framework | `pip install lovia` |
| DuckDuckGo search | `pip install "lovia[ddg]"` |
| MCP integration | `pip install "lovia[mcp]"` |
| Web UI | `pip install "lovia[web]"` |
| Runnable examples | `pip install "lovia[examples,web]"` |
| Development | `pip install -e ".[dev]"` |

`examples` contains dependencies used only by runnable demos, such as
`python-dotenv`, `rich`, `prefect`, and `ddgs`. `dev` contains repository
maintenance dependencies: `pytest`, `ruff`, `mypy`, `build`, `twine`, and the
web test stack. They stay separate so normal development does not install
demo-only packages like Prefect.

## Development

```bash
pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format .
.venv/bin/python -m mypy lovia
```

The `examples/` directory contains runnable scripts for the major features.
Live provider tests are marked `live_provider` and stay skipped unless enabled
explicitly.
