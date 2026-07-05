# lovia

[中文文档](./README-zh.md) · [Documentation](./docs/en/README.md) · [Examples](./examples/README.md)

lovia is an elegant, restrained Python framework for developers who want to
own the agent loop without rebuilding every supporting primitive from scratch.
It gives you the pieces most agent apps eventually need — tools, streaming,
structured output, sessions, handoff, approvals, guardrails, workspaces,
skills, MCP, context compaction, checkpoint/resume, and a tiny web UI — and
handles those recurring hard parts without turning into a platform.

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

Or serve a full chat UI — memory, skills, scheduling, and a workspace on
the current directory included — in one line:

```bash
pip install "lovia[web]" && python -m lovia.web
```

In the code above, `./skills` points at your team's skill directory; remove
`Skills("./skills")` until you have one. Set `OPENAI_API_KEY` for the
official OpenAI endpoint, or set `OPENAI_BASE_URL` for OpenAI-compatible
services such as DeepSeek, Ollama, or vLLM. Anthropic is built in too:
`model="anthropic:claude-4-8-opus"`.

## Documentation

This README is the tour. The [documentation](./docs/en/README.md) goes deep,
one feature per page — start with the
[quickstart](./docs/en/quickstart.md) and
[core concepts](./docs/en/concepts.md) — and the
[examples](./examples/README.md) are a numbered, runnable learning path.

## Why lovia

Composable primitives instead of a new universe of abstractions, staying
close to ordinary Python — dataclasses, protocols, async functions, explicit
composition:

- **Few abstractions.** An `Agent` is immutable configuration; a `Runner`
  executes one run; a `@tool` is a typed Python function; `Handoff` and
  `agent.as_tool()` compose agents; plugins package everything reusable.
- **Readable.** `lovia/runner.py` is a facade; the mutable run state lives in
  `lovia/runtime/loop.py`. When something surprises you, the path through the
  code is short.
- **Provider-neutral without an adapter tax.** Built-in providers speak OpenAI
  Chat Completions and Anthropic Messages directly over `httpx`; a custom
  provider is a `Protocol`, not a subclassing project.
- **Replaceable context management.** The default `Compaction` changes only
  what the model sees on the next call — the full transcript survives — and
  your own `ContextPolicy` can take over.
- **Atomic multi-agent.** Handoff transfers control; agent-as-tool delegates a
  subtask. Primitives, not an orchestration DSL you adopt wholesale.
- **Production seams, not a production costume.** Approvals, budgets,
  cancellation, mid-run steering, retries, hooks, scoped workspace tools, and
  checkpoint/resume are explicit knobs.
- **One extension axis.** Plugins bundle tools, prompt additions, per-turn
  view injectors, hooks, guardrails, and cleanup — Skills, MCP, Todo, and
  Memory all use the same mechanism.
- **Restraint as policy.** Concise over clever, lightweight over bundled,
  seams over lock-in; if a feature can be a short user-side recipe, it does
  not become framework surface area.

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

Two primitives, both ordinary tools underneath. **Handoff** transfers the
conversation — the specialist continues with the full history and answers
the user. **Agent-as-tool** delegates a bounded subtask — the child sees
only the prompt and its answer comes back as a tool result:

```python
from lovia import Agent, Runner

billing = Agent(name="billing", instructions="Handle billing issues.", model="deepseek-v4-pro")
support = Agent(name="support", instructions="Handle technical issues.", model="deepseek-v4-pro")

triage = Agent(
    name="triage",
    instructions="Route the user to the right specialist.",
    model="deepseek-v4-pro",
    handoffs=[billing, support],       # handoff: the specialist takes over
)
result = await Runner.run(triage, "I was charged twice.")
```

```python
summarizer = Agent(name="summarizer", instructions="Summarize text in five bullets.",
                   model="deepseek-v4-pro")

manager = Agent(
    name="manager",
    instructions="Delegate summarization when useful.",
    model="deepseek-v4-pro",
    tools=[summarizer.as_tool(description="Summarize a passage.")],  # delegate a subtask
)
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
injectors, hooks, and guardrails — never control flow:

```python
from lovia import Agent, Skills, Todo
from lovia.plugins.mcp import MCP, MCPServerStdio

agent = Agent(
    name="builder",
    model="deepseek-v4-pro",
    plugins=[
        Todo(),
        Skills("./skills"),
        MCP(MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])),
    ],
)
```

- **`Todo()`** — gives the model a checklist for multi-step work; the current
  list is re-shown every turn without growing the transcript.
- **`Skills(dir)`** — reusable instruction bundles (`SKILL.md` + files): a
  one-line index stays in the prompt, full content loads on demand.
- **`MCP(server)`** — tools from Model Context Protocol servers, stdio or
  HTTP, with per-server name prefixes and approval gates.

Writing your own is a `name` plus one async `setup()` returning the
contributions.

→ [Plugins](./docs/en/plugins.md) · [Skills](./docs/en/skills.md) · [MCP](./docs/en/mcp.md)

### Memory

Long-term memory across conversations — always-in-prompt **Notes** plus a
searchable **Archive**, zero-config, upgradeable one argument at a time:

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
schedules, a memory editor — whose runs survive browser disconnects:

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

```bash
python -m lovia.web --port 9000 --model deepseek-v4-pro   # or zero-config
```

The bundled page is optional: everything is exposed as a JSON + SSE REST
API (browse it at `/api/docs`), so `create_app(agent, ui=False)` — or
mounting the router into your own FastAPI app — lets you build a custom
front-end on the same endpoints.

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
