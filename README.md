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
  Qwen, …) just needs a `base_url`.
- 🛠 **Tools from anywhere.** `@tool` on a function, pydantic / dataclass /
  TypedDict / plain hints — all become JSON Schema automatically.
- 🧱 **Structured output.** Pass `output_type=YourModel`; uses native
  `response_format` when available, falls back to a synthetic tool otherwise.
- 🔁 **Streaming = events.** `run_stream` yields `TextDelta`, `ToolCallStarted`,
  `HandoffOccurred`, `RunCompleted`, … the same events go to hooks for
  observability.
- 🗣 **Handoffs & agent-as-tool.** Compose multi-agent systems without ceremony.
- 💾 **Sessions.** `InMemorySession` and `SQLiteSession`; plug your own
  `Session` implementation for Redis, Postgres, …
- 📚 **Skills.** Drop `SKILL.md` files in a directory; the agent lazy-loads them.
- 🌐 **MCP client.** Stdio + Streamable-HTTP via the official `mcp` SDK (optional).
- 🪝 **Hooks.** Subclass `AgentHooks`, plug into Logfire / OTel / your logger.

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
```

If nothing resolves the approval, the call is denied by default — runs never
hang on an absent decision.

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
with `OutputValidationError` instead.

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

### Observability

```python
from lovia import AgentHooks

class Logging(AgentHooks):
    async def on_tool_call_started(self, call): print("→", call.name)
    async def on_tool_call_completed(self, call, result, is_error):
        print("←", call.name, "error" if is_error else "ok")

agent = Agent(..., hooks=Logging())
```

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

## Public surface

The complete API:

```python
from lovia import (
    Agent, Runner, RunContext, RunResult, RunHandle,
    tool, Tool,
    Session, AgentHooks,
    ChatMessage, ToolCall, Usage,
    Provider, OpenAIChatProvider, ModelSettings,
    Skill, SkillCatalog, Handoff, agent_as_tool, drop_stale_tool_calls,
    events,
)
from lovia.stores import InMemorySession, SQLiteSession
```

That's it.

## Status

Early but usable. The shape of the public API is stable; internal modules may
move around as we add features.

## License

MIT.
