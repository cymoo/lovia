# Quickstart

Go from installation to Tools, streaming, and typed output. Every Python block
is a complete script: replace `<model>` with the model name configured for your
endpoint, save the block to a file, and run it.

## 1. Install and configure credentials

```bash
pip install lovia
export OPENAI_API_KEY="<api-key>"
```

Bare model names use the OpenAI-compatible adapter. For Anthropic, local
models, gateways, and custom Base URLs, copy the matching setup from
[Installation](installation.md#configure-a-model).

## 2. Run your first Agent

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="Answer concretely and concisely.",
    model="<model>",
)

result = agent.run_sync("Tell a joke only Python developers would enjoy.")
print(result.output)
```

`Agent` is reusable configuration; it does not store conversation state.
`run_sync()` is convenient in ordinary scripts. Async applications use
`await agent.run(...)` and execute the same RunLoop.

## 3. Give the Agent a Tool

`@tool` turns a typed function into a model-callable capability. Its signature
becomes JSON Schema and its docstring tells the model when to call it.

```python
from lovia import Agent, tool


@tool
def check_inventory(sku: str) -> str:
    """Look up the stock level for a product SKU."""
    return f"{sku}: 41 units in stock"


agent = Agent(
    name="shop-assistant",
    instructions="Use tools for factual inventory questions.",
    model="<model>",
    tools=[check_inventory],
)

result = agent.run_sync("Is SKU-1401 in stock? Add one buying tip.")
print(result.output)
print(f"turns={result.turns}, tokens={result.usage.total_tokens}")
```

If the model calls `check_inventory`, one Turn requests and runs the Tool; the
next Turn uses its result. See [Core concepts](concepts.md#run-vs-turn).

## 4. Stream text and Tool events

Use `Runner.stream()` when a UI or CLI should react before the final answer is
ready. A `RunHandle` is both an async event stream and an awaitable result.

```python
import asyncio

from lovia import Agent, Runner, events, tool


@tool
def check_inventory(sku: str) -> str:
    """Look up the stock level for a product SKU."""
    return f"{sku}: 41 units in stock"


async def main() -> None:
    agent = Agent(name="shop-assistant", model="<model>", tools=[check_inventory])
    handle = Runner.stream(agent, "What is the stock for SKU-1401?")

    async for event in handle:
        if isinstance(event, events.TextDelta):
            print(event.delta, end="", flush=True)
        elif isinstance(event, events.ToolCallStarted):
            print(f"\n[calling {event.call.name}]", flush=True)

    result = await handle.result()
    print(f"\n\n{result.usage.total_tokens} tokens")


asyncio.run(main())
```

The event stream ends with `RunCompleted` or `RunFailed`; call `result()` to
return the result or raise the stored failure. See [Streaming](streaming.md).

## 5. Return validated data

Pass a Pydantic model as `output_type` when downstream code needs an object
instead of prose.

```python
from pydantic import BaseModel

from lovia import Agent


class CityFact(BaseModel):
    city: str
    country: str
    population_millions: float


agent = Agent(
    name="researcher",
    instructions="Return current approximate figures.",
    model="<model>",
    output_type=CityFact,
)

result = agent.run_sync("Give me one fact record for Shanghai.")
print(result.output.city)
print(result.output.population_millions)
```

lovia validates the final answer and returns a `CityFact`. Provider-native JSON
Schema is used when available; otherwise lovia uses a portable Tool fallback.
See [Structured output](structured-output.md).

## 6. Open the chat UI

```bash
pip install "lovia[web]"
lovia web
```

Open `http://127.0.0.1:8000`. The first-run prompt can collect and save model
configuration. To serve an Agent defined in your own module, use
`lovia web --app mymodule:agent`; see [Web UI](web-ui.md).

## Choose your next step

| Goal | Guide | Example |
| --- | --- | --- |
| Persist a conversation | [Sessions & checkpoints](sessions-and-checkpoints.md) | [`05_sessions.py`](../../examples/05_sessions.py) |
| Add files and shell access | [Workspace](workspace.md) | [`20_workspace_agent.py`](../../examples/20_workspace_agent.py) |
| Require approval for side effects | [Tools: approval](tools.md#tool-approval) | [`12_approval.py`](../../examples/12_approval.py) |
| Add Skills, Todo, or Memory | [Plugins](plugins.md) | [Examples](../../examples/README.md) |
| Add retry and cost limits | [Provider retries](retries.md) · [Budgets](budgets.md) | [`14_reliability.py`](../../examples/14_reliability.py) |
| Test without network calls | [Testing](testing.md) | [`22_testing.py`](../../examples/22_testing.py) |
