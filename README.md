# lovia

**English** | [简体中文](./README-zh.md)

lovia is an elegant, restrained Python framework for developers who want to
own the agent loop without rebuilding every supporting primitive from scratch.
It covers the recurring hard parts of agent applications — tools, sessions,
events, context compaction, serving — without turning into a platform.

```bash
pip install lovia
```

```python
from lovia import Agent, tool


@tool
def get_order(order_id: str) -> str:
    """Look up an order's status by id."""
    return f"Order {order_id}: shipped 2 days ago, arriving Thursday."


# Configure OPENAI_BASE_URL and OPENAI_API_KEY in your environment.
agent = Agent(
    name="support",
    instructions="You are a customer-support agent. Look the order up before "
    "answering, and reply in one or two concrete sentences.",
    model="glm-5.2",
    tools=[get_order],
)

# run_sync() suits scripts and notebooks; from async code,
# use `await Runner.run(agent, ...)` instead.
result = agent.run_sync("Where is my order A-1042?")
print(result.output)
```

Or serve a full chat UI — memory, skills, scheduling, and a workspace on
the current directory included — in one line:

```bash
pip install "lovia[web]" && lovia web
```

Anthropic is built in too: configure `ANTHROPIC_API_KEY`, set
`ANTHROPIC_BASE_URL` for non-default endpoints, and use the `anthropic:`
model prefix. Everything else about models lives in
[Providers & models](https://cymoo.github.io/lovia/providers/).

## Documentation

This README is a quick tour. For the full guide, start with the
[quickstart](https://cymoo.github.io/lovia/quickstart/) and
[core concepts](https://cymoo.github.io/lovia/concepts/) in the
[documentation](https://cymoo.github.io/lovia/). The
[examples](./examples/README.md) are a numbered, runnable learning path.

## Why lovia

Composable primitives, ordinary Python — no new universe of abstractions:

- **Minimum dependencies.** The core depends only on `httpx`, `pydantic`,
  and `pyyaml`; install everything else only when needed.
- **Few abstractions.** An `Agent` is immutable configuration, a `Runner`
  executes one run, a `@tool` is a typed function; handoff and agent-as-tool
  compose agents; plugins package the rest.
- **Readable.** The critical path is concentrated and explicit: model
  calls, tool execution, retries, and persistence happen in a clear order.
  When something surprises you, there is one chain to follow.
- **Lightweight model integration.** OpenAI, Anthropic, and compatible
  endpoints are built in. There is no adapter stack to
  fight; a new provider is just a small `Protocol`.
- **Cache-friendly context management.** Compaction only changes what the
  model sees on the next call, keeping prompt prefixes stable while the
  complete record stays intact.
- **Production seams, not a production costume.** Approvals, budgets,
  cancellation, mid-run steering, retries, checkpoint/resume — explicit
  knobs you wire into your own app.
- **One extension axis.** Skills, MCP, Todo, and Memory are all plugins on
  the same seam you get for your own capabilities.

The design pressure throughout is restraint: if a feature can be a short
user-side recipe, it stays out of the framework.

## The tour

Every stop below has a full guide; each snippet runs as written.

### Agents

An `Agent` is declarative configuration — no conversation state, safe to
share, cheap to `clone()`. Tools, a workspace, and plugins compose directly on
that configuration:

```python
from lovia import Agent, Memory, Skills, tool
from lovia.workspace import Workspace


@tool
def light_travel_time(distance_km: float) -> str:
    """Calculate one-way light-signal delay for a distance in kilometers."""
    return f"{distance_km / 299_792.458:.2f} seconds"


agent = Agent(
    name="science-writer",
    instructions="Explain complex science with vivid, everyday analogies.",
    model="gpt-5.5",
    tools=[light_travel_time],
    workspace=Workspace.local(".", mode="readonly"),
    plugins=[
        Skills("./skills"),
        Memory(),
    ],
)
```

→ [Agents](https://cymoo.github.io/lovia/agents/)

### Running and streaming

One run, three consumption styles — and the stream handle is both
async-iterable and awaitable. Iteration never raises: every stream ends with
exactly one terminal event.

```python
from lovia import Runner, events

handle = Runner.stream(
    agent,
    "How long does a signal take to reach Mars at 225 million km?",
)

async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

→ [Running](https://cymoo.github.io/lovia/running/) · [Streaming](https://cymoo.github.io/lovia/streaming/)

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

→ [Tools](https://cymoo.github.io/lovia/tools/) · [Built-in tools](https://cymoo.github.io/lovia/built-in-tools/)

### Structured output

Pass a Pydantic model, dataclass, `TypedDict`, or plain type; the final
answer is validated — and repaired once, by default, when it doesn't parse:

```python
from pydantic import BaseModel
from lovia import Agent, Runner


class Brief(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(name="summarizer", model="glm-5.2", output_type=Brief)
result = await Runner.run(agent, "Summarize Transformer for a Python developer.")
print(result.output.title)
```

→ [Structured output](https://cymoo.github.io/lovia/structured-output/)

### Providers

Use model strings or provider instances. OpenAI-compatible
endpoints read `OPENAI_BASE_URL` / `OPENAI_API_KEY`; Anthropic defaults to
the official endpoint, reads `ANTHROPIC_API_KEY`, and uses
`ANTHROPIC_BASE_URL` for non-default endpoints. Prompt caching and reasoning
models are handled per endpoint, and a custom provider is a small
`Protocol`:

```python
from lovia import Agent, ModelSettings

agent = Agent(
    name="assistant",
    model="anthropic:<model>",
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
```

→ [Providers & models](https://cymoo.github.io/lovia/providers/)

### Multi-agent

Two primitives, both ordinary tools underneath. **Handoff** transfers the
conversation — the specialist continues with the full history and answers
the user. **Agent-as-tool** delegates a bounded subtask — the child sees
only the prompt and its answer comes back as a tool result:

```python
from lovia import Agent, Runner

billing = Agent(name="billing", instructions="Handle billing issues.", model="glm-5.2")
support = Agent(name="support", instructions="Handle technical issues.", model="glm-5.2")

triage = Agent(
    name="triage",
    instructions="Route the user to the right specialist.",
    model="deepseek-v4-flash",
    handoffs=[billing, support],       # handoff: the specialist takes over
)
result = await Runner.run(triage, "I was charged twice.")
```

```python
summarizer = Agent(name="summarizer", instructions="Summarize text in five bullets.",
                   model="glm-5.2")

manager = Agent(
    name="manager",
    instructions="Delegate summarization when useful.",
    model="deepseek-v4-flash",
    tools=[summarizer.as_tool(description="Summarize a passage.")],  # delegate a subtask
)
```

→ [Multi-agent](https://cymoo.github.io/lovia/multi-agent/)

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

→ [Tool approval](https://cymoo.github.io/lovia/tools/#tool-approval)

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

→ [Sessions & checkpoints](https://cymoo.github.io/lovia/sessions-and-checkpoints/)

### Context compaction

Long conversations survive the window without rewriting history: compaction
is view-only, keeps the prompt prefix stable for provider caches:

```python
from lovia import Agent, Compaction

agent = Agent(
    name="companion",
    model="glm-5.2",
    context_policy=Compaction(context_window=200_000, compact_at=0.85, compact_to=0.60),
)
```

→ [Context management](https://cymoo.github.io/lovia/context/)

### Guardrails and reliability

Guardrails veto runs at the input/output boundary. Reliability follows one
placement rule — retry posture on the agent, per-run limits on the run:

```python
from lovia import Agent, RetryPolicy, RunBudget, Runner
from lovia.exceptions import GuardrailTripped


async def must_cite(output, ctx):
    if "source:" not in str(output).lower():
        return "Missing source citation."


agent = Agent(
    name="researcher",
    model="glm-5.2",
    output_guardrails=[must_cite],
    retry=RetryPolicy(max_attempts=2)
)

result = await Runner.run(
    agent,
    "Analyze these logs.",
    budget=RunBudget(max_tool_calls=20, max_seconds=60)
)
```

→ [Guardrails](https://cymoo.github.io/lovia/guardrails/) · [Reliability](https://cymoo.github.io/lovia/reliability/)

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

→ [Observability](https://cymoo.github.io/lovia/observability/) · [Reliability](https://cymoo.github.io/lovia/reliability/#steering-a-live-run)

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
    model="glm-5.2",
    workspace=Workspace.local(
        ".",
        mode="coding",
        denied_paths=(".env*",),
        command_rules=(CommandRule("pytest", "allow"), CommandRule("rm -rf", "deny")),
    ),
)
```

→ [Workspace](https://cymoo.github.io/lovia/workspace/)

### Plugins

One extension axis: a plugin contributes tools, prompt text, per-turn view
injectors, hooks, and guardrails — never control flow:

```python
from lovia import Agent, Skills, Todo
from lovia.plugins.mcp import MCP, MCPServerStdio

agent = Agent(
    name="builder",
    model="glm-5.2",
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

→ [Plugins](https://cymoo.github.io/lovia/plugins/) · [Skills](https://cymoo.github.io/lovia/skills/) · [MCP](https://cymoo.github.io/lovia/mcp/)

### Memory

Long-term memory across conversations — always-in-prompt **Notes** plus a
searchable **Archive**, zero-config, upgradeable one argument at a time:

```python
from lovia import Agent, Memory
from lovia.plugins import OpenAIEmbedder

agent = Agent(name="assistant", model="glm-5.2",
              plugins=[Memory("./.lovia/memory")])

Memory("./memory")                             # stdlib keyword search (FTS5 bm25)
Memory("./memory", embedder=OpenAIEmbedder())  # + semantic arm -> hybrid recall
Memory("./memory", index=None)                 # notes only, no archive
```

→ [Memory](https://cymoo.github.io/lovia/memory/)

### Web UI

A lightweight FastAPI app — SSE streaming, sessions with titles, approvals,
schedules, a memory editor, image & file attachments — whose runs survive
browser disconnects:

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

```bash
lovia web --port 9000 --model glm-5.2      # python -m lovia.web works too
lovia web --model deepseek-v4-pro --base-url https://api.deepseek.com
```

Anything required but missing (Base URL, API key, model name)
is asked interactively on first run and can be saved to `./.env`.
Configuration precedence: flag > environment > `./.env` (or `--env-file`).

The bundled page is optional: everything is exposed as a JSON + SSE REST
API (browse it at `/api/docs`), so `create_app(agent, ui=False)` — or
mounting the router into your own FastAPI app — lets you build a custom
front-end on the same endpoints.

→ [Web UI](https://cymoo.github.io/lovia/web-ui/) · [Web server](https://cymoo.github.io/lovia/web-server/) · [HTTP API](https://cymoo.github.io/lovia/http-api/)

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

→ [Evals](https://cymoo.github.io/lovia/eval/)

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

→ [Testing](https://cymoo.github.io/lovia/testing/)

## Examples

The `examples/` directory is a numbered learning path of thirty
self-contained, runnable scripts — from `01_hello.py` to a terminal support
bot — plus one script per built-in tool family (`tools/`) and the classic
agentic patterns in plain Python (`workflows/`). Setup and the full index:
[examples/README.md](examples/README.md).

## Install extras

| Need | Install |
| --- | --- |
| Core framework | `pip install lovia` |
| DuckDuckGo search | `pip install "lovia[ddg]"` |
| Tavily search | no extra — set `TAVILY_API_KEY` |
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
[docs/architecture.md](https://cymoo.github.io/lovia/architecture/).
