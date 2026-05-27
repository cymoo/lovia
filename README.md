# lovia

A lightweight, provider-neutral agent framework for Python.

[简体中文](./README-zh.md)

```python
from lovia import Agent, Runner

agent = Agent(name="Greeter", instructions="Reply in one short line.", model="gpt-4o-mini")
result = await Runner.run(agent, "Say hi in three languages.")
print(result.output)
```

lovia’s core is a small set of orthogonal pieces — an `Agent` config, a
`Runner` that drives the loop, a `Provider` Protocol, and an Item-based
transcript. The provider layer speaks OpenAI Chat Completions, the OpenAI
Responses API, Anthropic, and anything OpenAI-compatible. Everything else —
tools, structured output, sessions, handoffs, guardrails, approval, MCP,
skills, memory, tracing — is opt-in.

- **No DSL, no graph, no implicit globals.** Plain Python with type hints.
- **Two required deps** in core: `httpx` and `pydantic`.
- **Async-first** API; synchronous helpers where they pay for themselves.

---

## Install

```bash
pip install lovia                 # core
pip install "lovia[mcp]"          # + Model Context Protocol client
pip install "lovia[web]"          # + FastAPI / SSE + bundled chat UI
pip install "lovia[dev]"          # + pytest, ruff, mypy
```

Requires Python 3.10+.

---

## Quickstart

A complete agent with a tool, in one file:

```python
import asyncio
from lovia import Agent, Runner, tool

@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

agent = Agent(
    name="Calc",
    instructions="Use the add tool when the user asks for arithmetic.",
    model="gpt-4o-mini",
    tools=[add],
)

async def main() -> None:
    result = await Runner.run(agent, "What is 17 + 25?")
    print(result.output)

asyncio.run(main())
```

`Runner.run` returns a `RunResult` with the final `output`, the new transcript
`new_items`, token `usage`, and `turns`. For streaming, use
`Runner.stream(agent, ...)` and iterate over events instead.

---

## Core concepts

### Agent

`Agent` is a dataclass — a piece of static configuration:

```python
Agent(
    name="Researcher",
    instructions="...",         # str, or a function(ctx) -> str
    model="gpt-4o-mini",        # provider:model, or just model (defaults to openai:)
    tools=[...],                # list[Tool]
    output_type=MyModel,        # optional pydantic model for structured output
    handoffs=[...],             # other Agents this one can hand off to
    input_guardrails=[...],     # validate input before the loop starts
    output_guardrails=[...],    # validate final output before returning
    hooks=...,                  # AgentHooks for observability
    model_settings=ModelSettings(temperature=0.2, ...),
)
```

`Agent` is `Generic[TContext]`. Pass a `context=` to `Runner.run` and tools
that take a typed `RunContext[TContext]` first parameter receive it.

### Runner

`Runner` is a stateless orchestrator. The two entry points:

- `await Runner.run(agent, input, *, context=None, session=None, ...)` —
  buffered; returns `RunResult`.
- `Runner.stream(agent, input, ...)` returns a `RunHandle`. Iterate
  `async for event in handle.events()` to receive structured events
  (`TextDelta`, `ToolCallStarted`, `MessageCompleted`, …), and `await
  handle.result()` for the final `RunResult`.

### Tools

Define a tool with the `@tool` decorator. Type annotations become the JSON
Schema; the docstring becomes the description.

```python
from dataclasses import dataclass
from lovia import RunContext, tool

@dataclass
class Deps:
    db: "Database"

@tool
async def lookup(ctx: RunContext[Deps], user_id: str) -> dict:
    """Look up a user by id."""
    return await ctx.context.db.get(user_id)
```

Tool *policies* are flat fields on `@tool`:

```python
@tool(
    needs_approval=True,         # gate behind ApprovalChannel
    retries=2,                   # retry on tool exceptions
    timeout=10.0,                # per-call timeout in seconds
    result_renderer=lambda r: r.summary,  # how the result is shown to the model
    wrap=my_middleware,          # escape hatch: (next, args, ctx) -> result
)
def risky(...): ...
```

For ad-hoc cases you can also construct `Tool(name=..., parameters=...,
invoke=...)` directly.

### Items: the transcript

A run produces a stream of typed *items* — the canonical conversation
record:

- `InputMessageItem` — user / system input.
- `MessageOutputItem` — assistant text.
- `ReasoningItem` — model reasoning (OpenAI Responses, etc.).
- `ToolCallItem` / `ToolCallOutputItem` — tool invocations and results.

Items are dataclasses with stable `to_dict` / `from_dict` helpers, suitable
for persistence. `result.messages` provides a Chat-style view derived from
the items, if you want that.

### Providers

A `Provider` adapts a vendor API to lovia’s Item-based streaming protocol.
The string passed to `Agent(model=...)` selects one:

| Prefix | Adapter |
| --- | --- |
| *(none)* or `openai:` | OpenAI Chat Completions |
| `openai-responses:` / `responses:` | OpenAI Responses API (reasoning items, server tools) |
| `anthropic:` | Anthropic Messages |
| Custom prefix | Anything you register |

For OpenAI-compatible endpoints (DeepSeek, Ollama, vLLM, …) construct a
provider explicitly:

```python
from lovia import OpenAIChatProvider

provider = OpenAIChatProvider(
    model="deepseek-chat",
    base_url="https://api.deepseek.com/v1",
    api_key=os.environ["DEEPSEEK_API_KEY"],
)
agent = Agent(name="...", model=provider)
```

Custom providers implement `Provider.stream(input: list[Item], ...) ->
AsyncIterator[ItemDelta]`. That’s the entire contract.

### Structured output

Set `output_type=` to a Pydantic model and `result.output` is an instance of
that model. lovia handles JSON Schema generation, prompt suffix, and a single
repair round-trip if the model returns invalid JSON. Override the repair
behaviour by passing an `OutputRepairStrategy`.

### Sessions

A `Session` persists transcript items across turns:

```python
from lovia.stores import SQLiteSession

session = SQLiteSession(path="chat.db", session_id="user-42")
await Runner.run(agent, "What did I ask earlier?", session=session)
```

Built-in stores: `InMemorySession`, `SQLiteSession`. The `Session` Protocol
is two methods (`load` / `append`) — plug in Redis, Postgres, etc., as you
need.

### Checkpoints and resume

`Runner.stream(..., checkpointer=...)` saves a `RunSnapshot` after each turn.
Resume later with `Runner.resume(snapshot, ...)`. Useful for long runs and
human-in-the-loop approval flows.

### Multi-agent: handoff + agent-as-tool

Two orthogonal patterns, both first-class, no graph DSL:

- **Handoff.** The current agent transfers control to another. Used for
  triage / specialist routing.
  ```python
  triage = Agent(name="Triage", handoffs=[Handoff(refunds), Handoff(billing)])
  ```
- **Agent-as-tool.** Call another agent like a function:
  ```python
  summarizer = Agent(name="Summarizer", ...)
  writer = Agent(name="Writer", tools=[agent_as_tool(summarizer, name="summarize")])
  ```

### Hooks and events

Observe a run by subscribing to events or by attaching `AgentHooks` to the
agent. The event stream is the same one streaming consumers read; hooks and
tracers just listen on a separate channel.

```python
from lovia import AgentHooks, events as ev

hooks = AgentHooks()
hooks.on(ev.ToolCallStarted, lambda e: print("tool:", e.call.name))
agent = Agent(..., hooks=hooks)
```

### Approval

For human-in-the-loop tool gating, mark tools `needs_approval=True` and
provide an `ApprovalChannel`. The run pauses on an `ApprovalRequired` event;
respond via the channel to continue or deny.

### Safety nets

- `RunBudget(max_turns=..., max_tokens=..., wall_clock=...)` — hard ceilings.
- `RetryPolicy` — retries provider errors with backoff and optional fallback
  providers.
- `CancelToken` — cooperatively cancel an in-flight run.
- `InputGuardrail` / `OutputGuardrail` — validators that trip with
  `GuardrailTripped`.

### Tracing

`ConsoleTracer` and `InMemoryTracer` ship in core; `NoopTracer` is the
default. Each run/turn/tool/handoff/model-call gets a span automatically.

```python
from lovia import ConsoleTracer
agent = Agent(..., tracer=ConsoleTracer())
```

For OpenTelemetry, write a thin `Tracer` adapter — the Protocol is three
methods.

### MCP, Skills, Memory

- **MCP** (`lovia[mcp]`): connect to Model Context Protocol servers and
  expose their tools to an agent.
- **Skills**: lazy-loaded prompt fragments (`SKILL.md` + assets) discovered
  from a directory, surfaced as a `SkillCatalog`.
- **Memory**: a long-term retrieval Protocol, decoupled from `Session`. Core
  ships the Protocol; bring your own backend.

---

## Examples

All runnable from the repo root (most require an `OPENAI_API_KEY`):

| File | Topic |
| --- | --- |
| `01_hello.py` | The minimal agent. |
| `02_tools.py` | Tool calling. |
| `03_streaming.py` | Event-stream consumption. |
| `04_structured_output.py` | Pydantic `output_type`. |
| `05_handoff.py` | Triage to specialist agents. |
| `06_agent_as_tool.py` | One agent invoking another as a tool. |
| `07_session.py` | Multi-turn with `SQLiteSession`. |
| `08_skills.py` | Filesystem-based skills. |
| `09_compat_provider.py` | DeepSeek / Ollama / vLLM via OpenAI-compatible. |
| `10_hooks.py` | Observability with `AgentHooks`. |
| `11_approval.py` | Human-in-the-loop approval. |
| `12_multimodal.py` | Image + text input. |
| `13_budget_and_cancel.py` | Budgets, retries, cancellation, fallback. |
| `14_guardrails.py` | Input / output guardrails. |
| `15_resume.py` | Checkpoint and resume. |
| `16_web_serve.py` | Bundled chat UI over SSE. |
| `17_responses_reasoning.py` | OpenAI Responses with reasoning items. |

---

## Status

Pre-1.0. The public surface listed in `lovia/__init__.py` is the API
contract; everything else is internal and may change. The framework is
unpublished and undergoing active design work; backwards-compat shims are
not added for breaking changes during this phase.

## License

MIT.
