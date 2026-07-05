# lovia

[中文文档](./README-zh.md) · [Documentation](./docs/en/README.md) · [Examples](./examples/README.md)

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
from lovia import Agent, Skills, Todo, tool
from lovia.workspace import Workspace


@tool
def lookup_ticket(ticket_id: str) -> str:
    """Look up an internal support ticket."""
    return f"{ticket_id}: waiting for customer reply"


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

# run_sync() drops the asyncio boilerplate for scripts and notebooks; from
# async code use `await Runner.run(agent, ...)` instead.
result = agent.run_sync(
    "Check ticket T-1001 and draft a reply using our team guidelines.",
)
print(result.output)
```

Here `./skills` points at your team's skill directory; remove
`Skills("./skills")` until you have one. Set `OPENAI_API_KEY` for the
official OpenAI endpoint, or set `OPENAI_BASE_URL` for OpenAI-compatible
services such as DeepSeek, Ollama, or vLLM. Anthropic is built in too:
`model="anthropic:claude-4-8-opus"`.

Or skip the code entirely — the zero-config playground serves a chat UI
with memory, skills, scheduling, and a workspace on the current directory:

```bash
pip install "lovia[web]"
python -m lovia.web
```

## Documentation

This README is the tour. The [documentation](./docs/en/README.md) goes deep,
one feature per page — start with the
[quickstart](./docs/en/quickstart.md) and
[core concepts](./docs/en/concepts.md) — and the
[examples](./examples/README.md) are a numbered, runnable learning path.

## Why lovia

lovia favors composable primitives over a new universe of abstractions. It
stays close to ordinary Python: dataclasses, protocols, async functions, and
explicit composition.

- **It is readable.** `lovia/runner.py` is a facade; the mutable run state
  lives in `lovia/runtime/loop.py`. When something surprises you, the path
  through the code is short.
- **It is provider-neutral without an adapter tax.** Built-in providers speak
  OpenAI Chat Completions and Anthropic Messages directly over `httpx`. A
  custom provider is a `Protocol`, not a subclassing project.
- **Context management is replaceable.** The default `Compaction` changes only
  what the model sees on the next call. Sessions and checkpoints keep the full
  transcript, and advanced users can provide their own `ContextPolicy`.
- **Multi-agent composition stays atomic.** Handoff transfers control to a
  specialist; agent-as-tool delegates a bounded subtask. Both are primitives,
  not an orchestration DSL you have to adopt wholesale.
- **It has production seams, not a production costume.** Approvals, budgets,
  cancellation, mid-run steering, retries, hooks, scoped workspace tools, and
  checkpoint/resume are explicit knobs you can wire into your own app.
- **It has one extension axis.** Plugins bundle tools, prompt additions,
  per-turn view injectors, hooks, guardrails, and cleanup. Skills, MCP, todo
  lists, and long-term memory all use the same mechanism.

## Start small, add only what you need

You can use lovia as a tiny wrapper around a model call, then add capabilities
only when the product asks for them.

| When you need... | Add... | Guide |
| --- | --- | --- |
| A quick script or notebook helper | `Agent.run_sync(...)` | [Running](./docs/en/running.md) |
| Tool calling | `@tool` functions (parallel by default) | [Tools](./docs/en/tools.md) |
| A tool whose side effects must not overlap | `@tool(parallel=False)` | [Tools](./docs/en/tools.md) |
| Typed final answers | `output_type=YourModel` | [Structured output](./docs/en/structured-output.md) |
| Live UI updates | `Runner.stream(...)` and typed events | [Streaming](./docs/en/streaming.md) |
| Multi-turn chat | `SQLiteSession` or your own `Session` | [Sessions](./docs/en/sessions-and-checkpoints.md) |
| Crash recovery, idempotent runs | `CheckpointOptions` | [Checkpoints](./docs/en/sessions-and-checkpoints.md#checkpoints) |
| Multi-agent routing or delegation | `handoffs=[...]` or `agent.as_tool()` | [Multi-agent](./docs/en/multi-agent.md) |
| Human approval | `@tool(needs_approval=True)` | [Human in the loop](./docs/en/human-in-the-loop.md) |
| Files and shell commands | `Workspace.local(...)` | [Workspace](./docs/en/workspace.md) |
| Long context survival | `Compaction` (auto-provides recall) | [Context](./docs/en/context.md) |
| Memory across conversations | `Memory(...)` | [Memory](./docs/en/memory.md) |
| Reusable capabilities | `PluginInstance`, `Skills`, `Todo`, or `MCP` | [Plugins](./docs/en/plugins.md) |
| Behavioral test suites | `lovia.eval` | [Evals](./docs/en/eval.md) |

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
  -> the run's own entries are appended to the session; the run is checkpointed
```

The boundaries that make this predictable — transcript vs view, session vs
checkpoint, posture vs limits — are the subject of
[core concepts](./docs/en/concepts.md).

## The tour

Every stop below has a full guide; each snippet runs as written.

### Agents

An `Agent` is declarative configuration — no conversation state, safe to
share, cheap to `clone()`. Prompt fragments can be dynamic per run:

```python
from lovia import Agent

agent = Agent(name="writer", instructions="Write concrete, concise answers.",
              model="deepseek-v4-pro")

@agent.instruction
async def user_tier(ctx) -> str:
    return f"User tier: {ctx.deps['tier']}"
```

→ [Agents](./docs/en/agents.md)

### Running and streaming

One run, three consumption styles — and the stream handle is both
async-iterable and awaitable. Iteration never raises: every stream ends with
exactly one terminal event.

```python
from lovia import Runner, events

handle = Runner.stream(agent, "Explain context windows in one paragraph.")

async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

→ [Running](./docs/en/running.md) · [Streaming](./docs/en/streaming.md)

### Tools

Typed Python functions; the schema comes from signatures, docstrings,
`Annotated`, and Pydantic `Field`s. One turn's calls execute concurrently by
default — tools with non-reentrant side effects opt out and become execution
barriers:

```python
from typing import Annotated
from pydantic import Field
from lovia import tool


@tool(strict=True)
def search_docs(
    query: Annotated[str, "Search terms"],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """Search internal documentation."""
    return []


@tool(parallel=False)
async def apply_migration(name: str) -> str:
    """Apply a database migration (never concurrently with other tools)."""
    return "applied"
```

→ [Tools](./docs/en/tools.md) · [Built-in tools](./docs/en/built-in-tools.md)

### Structured output

Pass a Pydantic model, dataclass, `TypedDict`, or plain type; the final
answer is validated — and repaired once, by default, when it doesn't parse:

```python
from pydantic import BaseModel
from lovia import Agent, Runner


class Brief(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(name="summarizer", model="deepseek-v4-pro", output_type=Brief)
result = await Runner.run(agent, "Summarize lovia for a Python developer.")
print(result.output.title)
```

→ [Structured output](./docs/en/structured-output.md)

### Providers

Model strings, provider instances, or a fallback chain; OpenAI-compatible
endpoints ride `OPENAI_BASE_URL`, prompt caching and reasoning models are
handled per host, and a custom provider is a small `Protocol`:

```python
from lovia import Agent, ModelSettings, model_from_env

agent = Agent(
    name="assistant",
    model=["anthropic:claude-4-8-opus", "deepseek-v4-pro"],  # fallback chain
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)

scripted = Agent(name="ci", model=model_from_env())  # LOVIA_MODEL, fail-loudly
```

→ [Providers & models](./docs/en/providers.md)

### Multi-agent

Two primitives, both ordinary tools underneath. Handoff transfers the
conversation to a specialist; agent-as-tool delegates a bounded subtask:

```python
from lovia import Agent, Runner

billing = Agent(name="billing", instructions="Handle billing issues.", model="deepseek-v4-pro")
support = Agent(name="support", instructions="Handle technical issues.", model="deepseek-v4-pro")

triage = Agent(
    name="triage",
    instructions="Route the user to the right specialist.",
    model="deepseek-v4-pro",
    handoffs=[billing, support],
    tools=[support.as_tool(description="Ask the tech specialist a question.")],
)

result = await Runner.run(triage, "I was charged twice.")
```

→ [Multi-agent](./docs/en/multi-agent.md)

### Human in the loop

Gate sensitive tools; resolve from your UI, a server-side policy, or an
out-of-band channel — unresolved approvals deny, so runs never hang:

```python
from lovia import Runner, events, tool


@tool(needs_approval=True)
async def refund(order_id: str, amount_cents: int) -> str:
    """Issue a refund."""
    return "refunded"


async for ev in Runner.stream(agent, "Refund order A123."):
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()          # or ev.reject()
```

→ [Human in the loop](./docs/en/human-in-the-loop.md)

### Sessions and checkpoints

Sessions persist conversations across runs; checkpoints make single runs
crash-safe and idempotent — re-issuing a completed `run_id` replays its
result without calling the model:

```python
from lovia import CheckpointOptions, Runner, SQLiteCheckpointer, SQLiteSession

session = SQLiteSession("chat.db")
await Runner.run(agent, "My project is called Atlas.", session=session, session_id="u1")

cp = SQLiteCheckpointer("runs.db")
result = await Runner.run(
    agent,
    "Migrate the report format.",
    checkpoint=CheckpointOptions(cp, "report-migration-42"),
)
```

→ [Sessions & checkpoints](./docs/en/sessions-and-checkpoints.md)

### Context compaction

Long conversations survive the window without rewriting history: compaction
is view-only, keeps the prompt prefix stable for provider caches, and
auto-provides `recall_tool_result` so the model can retrieve what the view
dropped:

```python
from lovia import Agent, Compaction

agent = Agent(
    name="companion",
    model="deepseek-v4-pro",
    context_policy=Compaction(context_window=200_000, compact_at=0.75, compact_to=0.50),
)
```

→ [Context management](./docs/en/context.md)

### Guardrails and reliability

Guardrails veto runs at the input/output boundary. Reliability follows one
placement rule — retry posture on the agent, per-run limits on the run:

```python
from lovia import Agent, RetryPolicy, RunBudget, Runner
from lovia.exceptions import GuardrailTripped


async def must_cite(output, ctx):
    if "source:" not in str(output).lower():
        return "Missing source citation."


agent = Agent(name="researcher", model="deepseek-v4-pro",
              output_guardrails=[must_cite],
              retry=RetryPolicy(max_attempts=2))            # posture

result = await Runner.run(agent, "Analyze these logs.",
                          budget=RunBudget(max_tool_calls=20, max_seconds=60))  # limits
```

→ [Guardrails](./docs/en/guardrails.md) · [Reliability](./docs/en/reliability.md)

### Hooks and steering

Hooks observe every run event (fail-open, same types as streaming); the
mailbox is the inbound dual of cancellation — push a message into a live run
and the model sees it next turn. A run can even steer itself:

```python
from lovia import Mailbox, RunContext, Runner, events
from lovia.hooks import AgentHooks

hooks = AgentHooks()


@hooks.on(events.TurnStarted)
def deadline(ev, ctx: RunContext):
    if ev.turn == 9:
        ctx.mailbox.push("Last turn: answer with what you have.")


mailbox = Mailbox()
handle = Runner.stream(agent.clone(hooks=hooks), "Analyze these logs.", mailbox=mailbox)
mailbox.push("Focus on the 5xx spike around 14:00.")  # seen next turn
```

→ [Observability](./docs/en/observability.md) · [Reliability](./docs/en/reliability.md#steering-a-live-run)

### Workspace

File and shell tools scoped to a root, governed by one `allow`/`ask`/`deny`
policy over paths *and* commands — `ask` decisions ride the normal approval
channel:

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
        command_rules=(CommandRule("pytest", "allow"), CommandRule("rm -rf", "deny")),
    ),
)
```

→ [Workspace](./docs/en/workspace.md)

### Plugins

One extension axis: a plugin contributes tools, prompt text, per-turn view
injectors, hooks, and guardrails — never control flow. Todo, Skills, and MCP
are all plugins:

```python
from lovia import Agent, Skills, Todo
from lovia.plugins.mcp import MCP, MCPServerStdio

agent = Agent(
    name="builder",
    model="deepseek-v4-pro",
    plugins=[
        Todo(),                      # externalized checklist, re-shown every turn
        Skills("./skills"),          # instruction bundles, loaded on demand
        MCP(MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])),
    ],
)
```

→ [Plugins](./docs/en/plugins.md) · [Skills](./docs/en/skills.md) · [MCP](./docs/en/mcp.md)

### Memory

Long-term memory from two tiers and three verbs: char-budgeted **Notes**
always in the prompt (`remember`/`forget`), a searchable **Archive** of past
conversations on demand (`recall`) — zero-config SQLite FTS5, escalating one
argument at a time:

```python
from lovia import Agent, Memory
from lovia.plugins import OpenAIEmbedder

agent = Agent(name="assistant", model="deepseek-v4-pro",
              plugins=[Memory("./.lovia/memory")])

Memory("./memory")                             # stdlib keyword search (FTS5 bm25)
Memory("./memory", embedder=OpenAIEmbedder())  # + semantic arm -> hybrid recall
Memory("./memory", index=None)                 # notes only, no archive
```

→ [Memory](./docs/en/memory.md)

### Web UI

A small FastAPI app — SSE streaming, sessions with titles, approvals,
schedules, a memory editor — whose runs survive browser disconnects. The
JSON + SSE API stands alone for your own front-end:

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

```bash
python -m lovia.web --port 9000 --model deepseek-v4-pro   # or zero-config
```

→ [Web UI & server](./docs/en/web.md) · [HTTP API](./docs/en/http-api.md)

### Evals

Behavioral test suites: a `Case` pairs input with checks, any function is a
check, the LLM judge is just another check, and reports diff against a
baseline in CI:

```python
from lovia.eval import Case, contains, evaluate, llm_judge, tool_called

report = await evaluate(agent, [
    Case("What is the capital of France?", checks=[contains("Paris")]),
    Case("What's 23.4 * 91?", checks=[tool_called("calculator")]),
    Case("Write a haiku about spring",
         checks=[llm_judge("A 5-7-5 haiku that evokes spring")],
         samples=4, pass_threshold=0.75),
])
print(report)
assert report.passed
```

→ [Evals](./docs/en/eval.md)

### Testing

Everything runs offline against a scripted provider — real tools, real loop,
canned model:

```python
from lovia.testing import ScriptedProvider, call, text

provider = ScriptedProvider([
    call("add", {"a": 2, "b": 3}, call_id="c1"),
    text("The answer is 5."),
])
```

→ [Testing](./docs/en/testing.md)

## Examples

The `examples/` directory is a numbered learning path of self-contained,
runnable scripts — `cp .env.example .env`, set `LOVIA_MODEL`, and start with
`01_hello.py`. See [examples/README.md](examples/README.md) for the full
index and setup notes.

| Section | Files | Covers |
| --- | --- | --- |
| Fundamentals | `01`–`06` | hello, tools, streaming, structured output, sessions, multimodal |
| Multi-agent | `07`–`08` | handoff, agent-as-tool |
| Models & providers | `09`–`10` | `ModelSettings`, compatible endpoints, custom `Provider` (offline) |
| Control & production | `11`–`18` | hooks, approval, guardrails, reliability, resume, steering, compaction, dependency injection |
| Workspace & plugins | `19`–`25` | workspace, coding agent, todos, skills, memory, MCP, writing a plugin |
| Serving & apps | `26`–`30` | web UI, JSON/SSE API, evals, data analysis, terminal support bot |
| `examples/tools/` | | one script per built-in tool family |
| `examples/workflows/` | | prompt chaining, routing, parallelization, orchestrator-workers, evaluator loops, autonomous agents |

## Install extras

| Need | Install |
| --- | --- |
| Core framework | `pip install lovia` |
| DuckDuckGo search | `pip install "lovia[ddg]"` |
| MCP integration | `pip install "lovia[mcp]"` |
| Web UI | `pip install "lovia[web]"` |
| Runnable examples | `pip install "lovia[examples,web]"` |
| Development | `pip install -e ".[dev]"` |

## Development

```bash
pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format .
.venv/bin/python -m mypy lovia
```

Live provider tests are marked `live_provider` and stay skipped unless
enabled explicitly. Contributor-facing internals are documented in
[docs/architecture.md](docs/architecture.md).
