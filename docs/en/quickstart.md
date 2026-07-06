# Quickstart

Three steps: install lovia, configure a model, and build an agent that can
call a tool. Every snippet below runs once your model is configured.

## Install

```bash
pip install lovia
```

Python 3.10+. The core depends only on `httpx`, `pydantic`, and `pyyaml`;
install extras such as the web UI, MCP, or search only when you need them.

## Configure a model

For the official OpenAI API and OpenAI-compatible endpoints such as
DeepSeek, Ollama, and vLLM, set `OPENAI_BASE_URL` and `OPENAI_API_KEY`,
then use the endpoint's bare model name, such as `model="glm-5.2"`.

```bash
export OPENAI_BASE_URL="https://..."
export OPENAI_API_KEY="sk-..."
```

For Anthropic, set `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY`, then use
the `anthropic:` prefix, for example `model="anthropic:<model>"`.

```bash
export ANTHROPIC_BASE_URL="https://..."
export ANTHROPIC_API_KEY="sk-ant-..."
```

More model forms are covered in [Providers & models](providers.md).

## Your first agent

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="Answer concretely and concisely.",
    model="glm-5.2",
)

result = agent.run_sync("Tell a joke only Python developers would enjoy.")
print(result.output)
```

Use `run_sync()` in scripts; use `await agent.run(...)` from async code.

## Add a tool

Add `@tool` to an ordinary Python function and the model can call it. The
schema comes from type hints and the docstring.

```python
from lovia import Agent, tool


@tool
def check_inventory(sku: str) -> str:
    """Look up the stock level for a product SKU."""
    return f"{sku}: 41 units in stock"


agent = Agent(
    name="shop-assistant",
    instructions="Help customers with product questions.",
    model="glm-5.2",
    tools=[check_inventory],
)

result = agent.run_sync("Do you have SKU-1401 in stock? Add one buying tip.")
print(result.output)
```

For concurrency, retries, timeouts, and approvals, see [Tools](tools.md).

## Stream output

For a UI, render events as they arrive:

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

The full event catalog is in [Streaming](streaming.md).

## Get typed output

Pass `output_type` when you want an object instead of a string:

```python
from pydantic import BaseModel

from lovia import Agent


class Availability(BaseModel):
    sku: str
    in_stock: bool
    units: int


agent = Agent(
    name="inventory",
    model="glm-5.2",
    tools=[check_inventory],
    output_type=Availability,
)

result = agent.run_sync("Is SKU-1401 available?")
print(result.output.units)
```

Details are in [Structured output](structured-output.md).

## Open a chat UI

```bash
pip install "lovia[web]"
python -m lovia.web
```

The default UI starts at `http://127.0.0.1:8000`. Point it at your own
agent with `python -m lovia.web --app mymodule:agent`.

## Next steps

| Goal | Read |
| --- | --- |
| Browse runnable scripts | [Examples](../../examples/README.md) |
| Understand what a run does | [Core concepts](concepts.md) |
| Persist multi-turn chats | [Sessions & checkpoints](sessions-and-checkpoints.md) |
| Add files and shell access | [Workspace](workspace.md) |
| Build a Web UI or HTTP API | [Web UI & server](web.md), [HTTP API](http-api.md) |
| Add approvals, budgets, and retries | [Human in the loop](human-in-the-loop.md), [Reliability](reliability.md) |
| Test and evaluate behavior | [Testing](testing.md), [Evals](eval.md) |
