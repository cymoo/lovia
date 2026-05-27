# lovia

A lightweight, provider-neutral agent framework for Python.

```python
import asyncio
from lovia import Agent, Runner, tool

@tool
async def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"{city}: sunny, 22°C"

agent = Agent(
    name="Assistant",
    instructions="Use tools when helpful. Answer briefly.",
    model="openai:gpt-4o-mini",
    tools=[get_weather],
)

result = asyncio.run(Runner.run(agent, "What's the weather in Lisbon?"))
print(result.output)
```

## Why

Most agent frameworks either:

- Tie themselves to one vendor's API (so you cannot swap models without rewriting), or
- Pile on so many abstractions that simple things stop being simple.

**lovia** keeps the good ideas — declarative agents, tool calling, handoffs,
sessions, skills, MCP — and throws out the rest. Core is under ~2000 lines.
Hard dependencies are only `httpx` and `pydantic`.

## Features

- 🪶 **Tiny core, no magic.** One `Agent` dataclass, one `Runner`, async only.
- 🔌 **Provider-neutral.** Built-in adapters for OpenAI Chat Completions and
  Anthropic Messages. Any OpenAI-compatible endpoint (DeepSeek, Ollama, vLLM,
  Qwen, …) just needs a `base_url`. Pass a **list** of providers for automatic
  fallback.
- 🛠 **Tools from anywhere.** `@tool` on a function, pydantic / dataclass /
  TypedDict / plain hints — all become JSON Schema automatically. Optional
  `before` / `after` middleware lets you redact, transform, or audit calls.
- 🧱 **Structured output.** Pass `output_type=YourModel`; uses native
  `response_format` when available, falls back to a synthetic tool otherwise.
- 🖼 **Multimodal.** `TextBlock` / `ImageBlock` content; both OpenAI and
  Anthropic adapters translate them transparently.
- 🧠 **Reasoning tokens.** A `ReasoningDelta` event surfaces Anthropic thinking
  blocks and DeepSeek / OpenAI reasoning models.
- 🔁 **Streaming = events.** `run_stream` yields `TextDelta`,
  `ToolCallStarted`, `ReasoningDelta`, `HandoffOccurred`, `RunCompleted`, … the
  same events go to hooks for observability.
- 🛡 **Production-ready safety nets.** `RunBudget` caps tokens / tool calls /
  wall-clock; `CancelToken` cooperatively cancels; `RetryPolicy` retries
  transient provider errors with backoff; `Guardrail`s veto inputs/outputs.
- 💾 **Sessions + checkpoints.** `InMemorySession`/`SQLiteSession` for chat
  history; `InMemoryCheckpointer`/`SQLiteCheckpointer` for per-turn snapshots
  with `Runner.resume(...)`.
- 🗣 **Handoffs & agent-as-tool.** Compose multi-agent systems without ceremony.
- 📚 **Skills.** Drop `SKILL.md` files in a directory; the agent lazy-loads them.
- 🌐 **MCP client.** Stdio + Streamable-HTTP via the official `mcp` SDK (optional).
- 🪝 **Hooks.** Subclass `AgentHooks`, plug into Logfire / OTel / your logger.
- 🖥 **Web layer (optional).** `from lovia.web import serve`: FastAPI + SSE +
  a bundled chat UI for any agent. Approval gates surface as buttons.

## Install

```bash
pip install -e .
# Optional: MCP support
pip install -e .[mcp]
```

Requires Python 3.10+.

## Quick tour

### Streaming

```python
from lovia import Runner, events

# Iterate the event stream as the run executes.
async for ev in Runner.run_stream(agent, "Tell me a joke"):
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

# Or use ``run_streamed``, which returns a ``RunHandle`` that is both
# async-iterable and awaitable — so you can stream events *and* get the
# final ``RunResult`` from the same call.
handle = Runner.run_streamed(agent, "Tell me a joke")
async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)
result = await handle.result()
```

### Human-in-the-loop approval

Tools declared with `needs_approval=True` pause the runner and emit an
`ApprovalRequired` event. Resolve it however you like:

```python
from lovia import Agent, Runner, tool, events

@tool(needs_approval=True)
async def send_email(to: str, body: str) -> str: ...

# Option 1: decide while streaming.
handle = Runner.run_streamed(agent, "email Alice")
async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()      # or ev.reject()

# Option 2: a programmatic handler on the agent.
async def policy(call, ctx):
    return call.name != "drop_database"

agent = Agent(name="ops", model=..., tools=[send_email], approval_handler=policy)

# Option 3: resolve out-of-band by ToolCall id (e.g. from an HTTP handler).
handle.approvals.approve(call_id)   # or .reject(call_id)
```

If nothing resolves the approval, the call is denied by default — runs never
hang on an absent decision.

### Typed dependencies (a.k.a. `RunContext`)

Pass any object into a run via `context=` and tools receive it through a
typed handle. The runner detects the handle by **type annotation**, not by
parameter name:

```python
from dataclasses import dataclass
from lovia import Agent, Runner, RunContext, tool

@dataclass
class Deps:
    db: Database
    user_id: int

@tool
async def lookup_orders(ctx: RunContext[Deps], status: str) -> list[str]:
    # ctx.context is typed as Deps; auto-completion works.
    return await ctx.context.db.orders(ctx.context.user_id, status=status)

agent = Agent[Deps](name="ops", model="openai:gpt-4o-mini", tools=[lookup_orders])
await Runner.run(agent, "show pending orders", context=Deps(db=..., user_id=42))
```

Plain tools (no `RunContext` parameter) don't see the context at all — the
model only ever sees parameters that aren't injected by the runner.

### Structured output

```python
from pydantic import BaseModel
from lovia import Agent, Runner

class Weather(BaseModel):
    city: str
    temp_c: float

agent = Agent(name="W", model="openai:gpt-4o-mini", output_type=Weather)
result = await Runner.run(agent, "weather in Tokyo")
print(result.output.temp_c)  # typed!
```

If the model returns something that can't be parsed, lovia re-prompts it
once to fix the output. Set `output_repair=False` on the agent to fail fast
with `OutputValidationError` instead, or pass a custom
`OutputRepairStrategy` for multi-attempt or localised repair prompts:

```python
from lovia import DefaultOutputRepair
agent = Agent(..., output_repair=DefaultOutputRepair(max_attempts=3))
```

### Handoffs

```python
from lovia import Agent, Handoff, drop_stale_tool_calls

billing = Agent(name="Billing", model="openai:gpt-4o-mini", instructions="...")
support = Agent(name="Support", model="openai:gpt-4o-mini", instructions="...")

triage = Agent(
    name="Triage",
    model="openai:gpt-4o-mini",
    # Bare agents are fine; wrap in ``Handoff`` to customise.
    handoffs=[
        billing,
        Handoff(target=support, input_filter=drop_stale_tool_calls),
    ],
)
```

### Sessions (multi-turn, multi-user)

```python
from lovia.stores import SQLiteSession

session = SQLiteSession("./conversations.db")
await Runner.run(agent, "Hi, I'm Mei",   session=session, session_id="user-mei")
await Runner.run(agent, "What's my name?", session=session, session_id="user-mei")
```

`session_id` is yours — use a user id, conversation id, or anything else.

### Any OpenAI-compatible model

```python
from lovia import Agent, OpenAIChatProvider

provider = OpenAIChatProvider(
    model="deepseek-chat",
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com/v1",
)
agent = Agent(name="DS", model=provider, instructions="...")
```

### Plug in a new provider

Third-party providers can be registered under a custom vendor prefix and
become usable from `"vendor:model"` strings:

```python
from lovia.providers import register_provider

register_provider("gemini", lambda model: MyGeminiProvider(model=model))

agent = Agent(name="G", model="gemini:gemini-1.5-pro", instructions="...")
```

The same mapping can be exposed declaratively as a `lovia.providers`
entry-point group in your package metadata — `provider_from_string`
discovers installed plugins lazily on first use.

### Multimodal content

```python
from lovia import Agent, Runner, TextBlock, ImageBlock
from lovia.messages import user

msg = user([
    TextBlock(text="What's in this picture?"),
    ImageBlock(url="https://example.com/cat.jpg"),
])
result = await Runner.run(agent, [msg])
```

Image blocks accept either a `url` or base64 `data` + `media_type`; both
OpenAI and Anthropic adapters translate them to vendor formats.

### Budget, retry, cancel, provider fallback

```python
from lovia import Agent, Runner, RunBudget, RetryPolicy, CancelToken

budget = RunBudget(max_output_tokens=10_000, max_tool_calls=50, max_seconds=300)
retry  = RetryPolicy(max_attempts=3)
cancel = CancelToken()

# A list of providers turns into an automatic fallback chain.
agent = Agent(
    name="resilient",
    model=["openai:gpt-4o-mini", "anthropic:claude-3-5-haiku-latest"],
)

result = await Runner.run(
    agent, "summarise this 30-page doc...",
    budget=budget, retry=retry, cancel_token=cancel,
)
```

Cancel mid-run from a signal handler / UI thread: `cancel.cancel()`.

### Guardrails

```python
from lovia import Agent, GuardrailTripped

async def block_pii(messages, ctx):
    if any("ssn" in (m.text or "").lower() for m in messages):
        return "input contains PII"

async def require_citation(output, ctx):
    if "[source]" not in (output or ""):
        return "answer must cite a source"

agent = Agent(
    name="careful",
    model="openai:gpt-4o-mini",
    input_guardrails=[block_pii],
    output_guardrails=[require_citation],
)
```

Either type of guardrail can return a reason string (or `True`) to veto;
return `None`/`False` to allow. Sync and async guardrails both work.

### Checkpoint & resume

```python
from lovia import Runner
from lovia.stores import SQLiteCheckpointer  # also: InMemoryCheckpointer

cp = SQLiteCheckpointer("./runs.sqlite")

# Snapshots are written at the end of every turn.
await Runner.run(agent, "long-running task...", checkpointer=cp, run_id="run-42")

# Later — possibly in a different process — pick up where it left off.
result = await Runner.resume(agent, checkpointer=cp, run_id="run-42")
```

The opaque `context` value is *not* snapshotted; re-supply it on `resume`.

### Tool policies

```python
from lovia import tool

# Per-tool retries + timeout, applied around each attempt.
@tool(retries=3, timeout=10)
async def search(query: str) -> list[str]: ...

# Custom result rendering controls the string the model sees.
@tool(result_renderer=lambda r, ctx: f"{len(r)} results")
async def find(q: str) -> list[str]: ...

# ``wrap`` is the single escape hatch for the rare case where the flat
# fields aren't enough (caching, custom auth, mocking, ...). Retries and
# timeout, when configured, are applied *around* wrap.
async def cache(invoke, args, ctx):
    if hit := CACHE.get(args["q"]):
        return hit
    out = await invoke(args, ctx)
    CACHE[args["q"]] = out
    return out

@tool(wrap=cache)
async def expensive_lookup(q: str) -> str: ...
```

Agent-wide defaults (`default_tool_retries`, `default_tool_timeout`) apply
to any tool whose own field is left as `None`.

### Observability

Out of the box, attach a tracer to see the run / model_call / tool / handoff
tree:

```python
import logging
from lovia import Agent, ConsoleTracer

logging.basicConfig(level=logging.INFO)
agent = Agent(..., tracer=ConsoleTracer())
# Logs lines like:
#   run (212.3ms) agent='triage' turns=2 total_tokens=148
#     model_call (95.1ms) model='openai:gpt-4o-mini' turn=1
#     tool (1.2ms) name='add' call_id='c1'
#     model_call (78.4ms) model='openai:gpt-4o-mini' turn=2
```

`InMemoryTracer` records spans in `tracer.spans` for tests. The `Tracer`
Protocol is two methods (`span` + a `Span` with `set_attribute` /
`record_exception`) — wire an OpenTelemetry / Logfire backend by writing a
thin adapter.

For finer-grained hooks (per-event callbacks instead of spans), build an
event subscriber:

```python
from lovia import AgentHooks, events

hooks = AgentHooks()

@hooks.on(events.ToolCallStarted)
async def starting(ev): print("→", ev.call.name)

@hooks.on(events.ToolCallCompleted)
async def done(ev): print("←", ev.call.name, "error" if ev.is_error else "ok")

# A single handler may listen to several event types:
@hooks.on((events.RunCompleted, events.ErrorOccurred))
def at_end(ev): print("end:", type(ev).__name__)

agent = Agent(..., hooks=hooks)
```

### Web (REST + chat UI)

Optional. Installs FastAPI, uvicorn and `sse-starlette`:

```bash
pip install -e .[web]
```

Serve any agent over HTTP with a built-in chat page:

```python
from lovia import Agent, tool
from lovia.web import serve

@tool
async def add(a: float, b: float) -> float: return a + b

agent = Agent(name="lovia", model="gpt-4o-mini", tools=[add])
serve(agent)                          # → http://127.0.0.1:8000
```

Or compose the FastAPI app yourself:

```python
from lovia.web import create_app
app = create_app({"writer": a, "researcher": b})  # multi-agent
```

Endpoints: `GET /` (chat UI), `GET /api/agents`, `POST /api/chat`,
`POST /api/chat/stream` (SSE), `POST /api/chat/approve`,
`GET|DELETE /api/sessions/{id}`, `GET /healthz`, `GET /api/docs`.
Approval-gated tools surface live in the UI with Allow/Deny buttons.
The chat page is decoupled vanilla HTML/CSS/JS — no build step.

## Examples

See [`examples/`](./examples) for runnable scripts covering every feature:

| File | What it shows |
| --- | --- |
| `01_hello.py` | Minimal agent |
| `02_tools.py` | Sync + async tools |
| `03_streaming.py` | Consuming the event stream |
| `04_structured_output.py` | `output_type=BaseModel` |
| `05_handoff.py` | Triage → specialist handoffs |
| `06_agent_as_tool.py` | An agent invoked as a tool |
| `07_session.py` | `SQLiteSession` across turns |
| `08_skills.py` | Lazy-loaded `SKILL.md` skills |
| `09_compat_provider.py` | DeepSeek / Ollama via OpenAI-compat |
| `10_hooks.py` | `AgentHooks` for observability |
| `11_approval.py` | Human-in-the-loop tool approval |
| `12_multimodal.py` | Sending an image with `ImageBlock` |
| `13_budget_and_cancel.py` | `RunBudget`, `RetryPolicy`, `CancelToken` |
| `14_guardrails.py` | Input + output guardrails |
| `15_resume.py` | Checkpointing and resuming a run |
| `16_web_serve.py` | `lovia.web.serve` — REST + SSE + chat UI |

## Public surface

The complete API:

```python
from lovia import (
    Agent, Runner, RunContext, RunResult, RunHandle,
    tool, Tool,
    Session, AgentHooks,
    ChatMessage, ToolCall, Usage,
    TextBlock, ImageBlock, ContentBlock,
    Provider, OpenAIChatProvider, ModelSettings,
    RunBudget, RetryPolicy, CancelToken,
    InputGuardrail, OutputGuardrail, GuardrailTripped,
    Checkpointer, InMemoryCheckpointer, RunSnapshot,
    Skill, SkillCatalog, Handoff, agent_as_tool, drop_stale_tool_calls,
    events,
)
from lovia.stores import InMemorySession, SQLiteSession, SQLiteCheckpointer
from lovia.web import serve, create_app   # optional, requires `lovia[web]`
```

That's it.

## Status

Early but usable. The shape of the public API is stable; internal modules may
move around as we add features.

## License

MIT.
