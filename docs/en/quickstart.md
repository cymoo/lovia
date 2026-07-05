# Quickstart

From zero to a streaming, tool-using agent in about ten minutes. Every
snippet on this page runs as-is once your model is configured.

## Install

```bash
pip install lovia
```

Python 3.10+. The core install is deliberately small (`httpx`, `pydantic`,
`pyyaml`); everything else — web UI, MCP, search — is an
[optional extra](#next-steps).

## Configure a model

lovia has no default vendor: you say which model to use, and the provider
reads its own environment variables for credentials. The shortest path:

```bash
export OPENAI_API_KEY="sk-..."
```

That covers `model="openai:gpt-5.5"` against the official OpenAI API. Two
common variations:

- **OpenAI-compatible services** (DeepSeek, Ollama, vLLM, ...): also set
  `OPENAI_BASE_URL` and name the model bare — `model="deepseek-v4-pro"`
  routes to the OpenAI-compatible provider.
- **Anthropic**: set `ANTHROPIC_API_KEY` and use
  `model="anthropic:claude-4-8-opus"`.

Everything else about providers — fallback chains, sampling settings, custom
adapters — lives in [Providers & models](providers.md).

## Hello, agent

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="Answer concretely and concisely.",
    model="openai:gpt-5.5",  # or "deepseek-v4-pro", "anthropic:claude-4-8-opus", ...
)

result = agent.run_sync("Say hello in one sentence.")
print(result.output)
```

`run_sync()` is for scripts and REPLs — it owns the event loop. From async
code, `await agent.run(...)` instead (calling `run_sync` inside a running
event loop raises a `UserError` telling you exactly that).

An `Agent` is just configuration — it holds no conversation state, so one
instance is safe to share across requests. To avoid hard-coding the model
string, `lovia.model_from_env()` reads `LOVIA_MODEL` from the environment and
fails with a setup hint when nothing is configured; the examples all use it.

## Give it a tool

Any typed Python function becomes a tool. The schema the model sees is
derived from the signature and docstring — no separate definition to keep in
sync.

```python
from lovia import Agent, tool


@tool
def check_inventory(sku: str) -> str:
    """Look up the stock level for a product SKU."""
    return f"{sku}: 41 units in stock"


agent = Agent(
    name="shop-assistant",
    instructions="Help customers with product questions.",
    model="openai:gpt-5.5",
    tools=[check_inventory],
)

result = agent.run_sync("Do you have SKU-1401 in stock?")
print(result.output)
```

Tools can be sync (run in a worker thread) or `async def` (awaited
directly). When the model requests several calls in one turn they run
concurrently by default — see [Tools](tools.md) for opting out, retries,
timeouts, and approval gates.

## Stream it

For a UI you want events as they happen, not a result at the end:

```python
import asyncio

from lovia import Runner, events


async def main() -> None:
    handle = Runner.stream(agent, "What's in stock for SKU-1401?")

    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.ToolCallStarted):
            print(f"\n[calling {ev.call.name}...]")

    result = await handle.result()
    print(f"\n\n({result.usage.total_tokens} tokens)")


asyncio.run(main())
```

The handle is async-iterable (typed events) *and* awaitable (the final
`RunResult`). Iteration never raises — every stream ends with exactly one
terminal event — while `handle.result()` returns the result or raises the
run's error. The full event catalog is in [Streaming](streaming.md).

## Get typed output

Pass a Pydantic model (or dataclass, `TypedDict`, plain type) and the final
answer is parsed and validated for you:

```python
from pydantic import BaseModel

from lovia import Agent


class Availability(BaseModel):
    sku: str
    in_stock: bool
    units: int


agent = Agent(
    name="inventory",
    model="openai:gpt-5.5",
    tools=[check_inventory],
    output_type=Availability,
)

result = agent.run_sync("Is SKU-1401 available?")
print(result.output.units)  # a validated int, not a string to parse
```

If the model's answer doesn't validate, lovia asks it once to repair the
response before giving up. Details in
[Structured output](structured-output.md).

## Instant playground

You don't need to write serving code to chat with an agent:

```bash
pip install "lovia[web]"
python -m lovia.web
```

That builds a default agent — model from the environment, skills from
`./skills` if present, long-term memory, a todo list, and a workspace on the
current directory — and serves a chat UI at `http://127.0.0.1:8000`. Point it
at your own agent with `python -m lovia.web --app mymodule:agent`. See
[Web UI & server](web.md).

## Next steps

- **[Core concepts](concepts.md)** — ten minutes that make every other page
  shorter: what a run actually does, and where state lives.
- **[Examples](../../examples/README.md)** — a numbered learning path
  (`01_hello.py` ... `30_support_bot.py`). Setup:

  ```bash
  pip install -e ".[examples,web]"   # from a repo checkout
  cp .env.example .env               # set LOVIA_MODEL + your API key
  python examples/01_hello.py
  ```

- **Install extras** as you need them:

  | Need | Install |
  | --- | --- |
  | Web UI + HTTP API | `pip install "lovia[web]"` |
  | MCP servers | `pip install "lovia[mcp]"` |
  | DuckDuckGo search | `pip install "lovia[ddg]"` |

- Then dip into the [guides](README.md#guides) as features come up: sessions
  for multi-turn chat, approvals for sensitive tools, compaction for long
  conversations, evals for CI.
