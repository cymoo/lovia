# lovia

[中文文档](./README-zh.md)

lovia is an elegant, restrained Python framework for developers who want to
own the agent loop without rebuilding every supporting primitive from scratch.
It gives you the pieces most agent apps eventually need — tools, streaming,
structured output, sessions, handoff, approvals, guardrails, workspaces,
skills, MCP, context compaction, checkpoint/resume, and a tiny web UI — while
keeping the core direct enough to read, replace, and extend.

The core abstractions are few:

- an `Agent` is immutable configuration;
- a `Runner` executes one run;
- a `@tool` is just a typed Python function;
- `Handoff` and `agent.as_tool()` are the two atomic ways to compose agents;
- plugins package reusable capability without taking over control flow. MCP,
  Skills, Todo, and long-term memory can all be expressed as plugins.

That is the tradeoff: handle the recurring hard parts of agent applications,
but avoid turning the framework into a platform.

```bash
pip install lovia
```

```python
import asyncio
from lovia import Agent, Runner, Skills, Todo, tool
from lovia.workspace import Workspace


@tool
def lookup_ticket(ticket_id: str) -> str:
    """Look up an internal support ticket."""
    return f"{ticket_id}: waiting for customer reply"


async def main() -> None:
    agent = Agent(
        name="operator",
        instructions=(
            "You are a customer-support operator. "
            "Before replying, confirm the ticket state, then use team policy "
            "to give a clear, restrained, actionable response."
        ),
        model="deepseek-v4-pro",
        tools=[lookup_ticket],
        plugins=[Todo(), Skills("./skills")],
        workspace=Workspace.local(".", mode="trusted"),
    )
    result = await Runner.run(
        agent,
        "Check ticket T-1001 and draft a reply using our team guidelines.",
    )
    print(result.output)


asyncio.run(main())
```

Here `./skills` points at your team's skill directory; remove
`Skills("./skills")` until you have one.

Set `OPENAI_API_KEY` for the official OpenAI endpoint, or set
`OPENAI_BASE_URL` for OpenAI-compatible services such as DeepSeek, Ollama, or
vLLM. Anthropic is built in too:
`model="anthropic:claude-4-8-opus"`.

## Why lovia

lovia favors composable primitives over a new universe of abstractions. It
stays close to ordinary Python: dataclasses, protocols, async functions, and
explicit composition.

- **It is readable.** `lovia/runner.py` is a facade; the mutable run state lives
  in `lovia/runtime/loop.py`. When something surprises you, the path through
  the code is short.
- **It is provider-neutral without an adapter tax.** Built-in providers speak
  OpenAI Chat Completions and Anthropic Messages directly over `httpx`. A custom
  provider is a `Protocol`, not a subclassing project.
- **Context management is replaceable.** The default `Compaction` changes only
  what the model sees on the next call. Sessions and checkpoints keep the full
  transcript, and advanced users can provide their own `ContextPolicy`.
- **Multi-agent composition stays atomic.** Handoff transfers control to a
  specialist agent; agent-as-tool delegates a bounded subtask. Both are
  primitives, not an orchestration DSL you have to adopt wholesale.
- **It has production seams, not a production costume.** Approvals, budgets,
  cancellation, retries, hooks, scoped workspace tools, and checkpoint/resume
  are explicit knobs you can wire into your own app.
- **It has one extension axis.** Plugins bundle tools, prompt additions,
  per-turn view injectors, hooks, guardrails, and cleanup. Skills, MCP, todo
  lists, and long-term memory can use the same mechanism.

## Start small, add only what you need

You can use lovia as a tiny wrapper around a model call, then add capabilities
only when the product asks for them.

| When you need... | Add... |
| --- | --- |
| A quick script or notebook helper | `Agent.run_sync(...)` |
| Tool calling | `@tool` functions |
| Typed final answers | `output_type=YourModel` |
| Live UI updates | `Runner.stream(...)` and typed events |
| Multi-turn chat | `SQLiteSession` or your own `Session` |
| Long-running work | `CheckpointOptions` |
| Multi-agent routing or delegation | `handoffs=[...]` or `agent.as_tool()` |
| Human approval | `@tool(needs_approval=True)` |
| Files and shell commands | `Workspace.local(...)` |
| Long context survival | `Compaction` plus optional `recall_tool_result` |
| Custom context behavior | implement your own `ContextPolicy` |
| Reusable capabilities | `PluginInstance`, `Skills`, `Todo`, or `MCP` |

## Philosophy

lovia optimizes for four things, in this order. The order matters.

1. **Concise.** A feature should fit in your head. The public surface should
   be obvious, and internals should be readable when you need to debug.
2. **Lightweight.** The core should import quickly, install cleanly, and avoid
   dragging in infrastructure you did not ask for.
3. **Extensible.** Real applications need their own providers, storage,
   policies, tools, and UI. lovia gives you seams instead of lock-in.
4. **General-purpose.** The built-ins are practical, but not magical. They are
   examples of the same extension points you can use yourself.

The design pressure is restraint. If a feature can be a short user-side recipe,
it should not become framework surface area. If it belongs in the framework, it
should compose with the existing loop instead of creating a new one.

## How the pieces fit

Each run follows the same shape:

```text
Agent + input
  -> RunLoop loads session/checkpoint state
  -> plugins contribute tools, instructions, hooks, guardrails, view injectors
  -> context policy renders a per-call model view
  -> provider streams typed deltas
  -> tools, approvals, handoff, guardrails, and hooks run at explicit checkpoints
  -> full transcript is persisted back to session/checkpoint
```

Two boundaries are worth remembering:

- **Session vs checkpoint.** A `Session` is conversation memory across calls.
  A checkpoint is a crash-recovery snapshot for one idempotent run.
- **Transcript vs view.** The transcript is the source of truth. Context
  compaction only renders a smaller view for the provider, so long
  conversations can keep moving without rewriting history.

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

For scripts and REPLs you can call the agent directly:

```python
result = agent.run_sync("Summarize this file.")
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
        "anthropic:claude-4-8-opus",
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
agent = Agent(
    ...,
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
from lovia.tools.search import duckduckgo_search

agent = Agent(
    name="researcher",
    model="deepseek-v4-pro",
    tools=[http_fetch, duckduckgo_search()],
)
```

Install DuckDuckGo search support with:

```bash
pip install "lovia[ddg]"
```

Custom search is just a `WebSearch` implementation passed to `web_search()`.

## Plugins

A **plugin** is lovia's one extension axis for bundling a feature.

A single object contributes any mix of: `tools`, system-prompt `instructions`, per-turn
`view_injectors` (transient reminders, never written to the transcript), event
`hooks`, and `input_guardrails` / `output_guardrails`.

The runner activates each plugin **once per run** (and once per agent on a handoff) by awaiting its async
`setup()`, and releases anything it opened via `aclose()` when the run ends.

Plugins are purely additive — they never drive control flow; the loop keeps the
abort, retry, and handoff. Skills, MCP, and the todo list below are all built-in
plugins.

### Todo lists

The built-in todo plugin gives the model a checklist tool and re-shows the
current list every turn, without bloating the persisted transcript:

```python
from lovia import Agent, Runner, Todo

agent = Agent(
    name="builder",
    instructions="Complete multi-step work carefully.",
    model="deepseek-v4-pro",
    plugins=[Todo()],
)

await Runner.run(agent, "Implement a small REST API with tests and docs.")
```

### Skills

Skills are reusable instruction bundles following the Agent Skills
specification. lovia exposes skill metadata up front, then lets the model load
full instructions and referenced files only when needed.

```python
from lovia import Agent, Skills

agent = Agent(
    name="support",
    instructions="Help customers using the right policy.",
    model="deepseek-v4-pro",
    plugins=[Skills("./skills")],
)
```

A skill directory holds `SKILL.md` with YAML frontmatter, plus optional
`references/`, `scripts/`, and `assets/` files. Pass several directories, or
scope the catalog with a filter:

```python
plugins=[Skills("./skills", "./team-skills")]
plugins=[Skills("./skills", filter=lambda meta: "internal" not in meta.extra.get("tags", []))]
```

For a custom backend, pass a `SkillSource` (or a pre-built `SkillCategory`) instead of
paths.

### MCP

[Model Context Protocol](https://modelcontextprotocol.io) servers expose their
tools to the agent. Install the optional dependency:

```bash
pip install "lovia[mcp]"
```

```python
from lovia import Agent
from lovia.plugins.mcp import MCPServerStdio, MCP

agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    plugins=[
        MCP(MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"]))
    ],
)
```

By default each run opens and closes the server. To reuse one connection across
runs, open a session and pass the live connection instead:

```python
server = MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])

async with server.session() as conn:
    agent = Agent(name="assistant", model="deepseek-v4-pro", plugins=[MCP(conn)])
    await Runner.run(agent, "Fetch https://example.com and summarize it.")
```

`MCP()` takes several servers — `MCP(a, b)` — and `MCPServer.name` prefixes a
server's tools (`web__fetch`) to keep names unique.

### Writing a plugin

A plugin is any object with a `name` and an `async setup()` that returns a `PluginInstance`.

State that should be **fresh per run** is built inside `setup`
(like the todo list above); state that should **persist across runs and
sessions** is held on the plugin and passed in at construction.

Here is a long-term memory plugin — it wraps a backend you supply, created once and shared
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

## Examples

The `examples/` directory is a set of runnable scripts. A useful reading order:

| Path | What it shows |
| --- | --- |
| `examples/01_hello.py` | minimal agent |
| `examples/02_tools.py` | tool calling |
| `examples/03_streaming.py` | streaming events |
| `examples/04_structured_output.py` | validated output |
| `examples/05_handoff.py` | specialist handoff |
| `examples/06_agent_as_tool.py` | delegate to an agent as a tool |
| `examples/07_session.py` | persisted chat history |
| `examples/08_skills.py` | reusable skill instruction bundles |
| `examples/10_hooks.py` | lifecycle event hooks |
| `examples/11_approval.py` | human-in-the-loop approval |
| `examples/14_guardrails.py` | input/output guardrails |
| `examples/15_resume.py` | checkpoint and resume |
| `examples/16_web_serve.py` | built-in web UI |
| `examples/18_context_policy.py` | view-only context compaction |
| `examples/21_dx.py` | sync calls, per-call output types, and other DX shortcuts |
| `examples/23_workspace_agent.py` | scoped coding workspace |
| `examples/25_data_analysis.py` | data analysis agent |
| `examples/26_mcp.py` | MCP server tools |
| `examples/27_todos.py` | todo plugin and per-turn reminders |
| `examples/workflows/` | prompt chaining, routing, parallelization, evaluator loops, autonomous agents |

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
