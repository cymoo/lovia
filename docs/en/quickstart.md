# Quickstart

Build an agent with one typed tool, run it, and inspect the result. Each Python
block on this page is self-contained and can be copied into a fresh file.

## 1. Install and configure a model

```bash
pip install lovia
```

Set `LOVIA_MODEL` plus the credentials and Base URL required by your endpoint.
The [installation guide](installation.md#configure-a-model) has ready-to-edit
configurations for OpenAI, Anthropic, OpenAI-compatible,
Anthropic-compatible, and Ollama endpoints.

## 2. Run an Agent

```python
from lovia import Agent, Runner, model_from_env

agent = Agent(
    name="assistant",
    instructions="Answer concretely and concisely.",
    model=model_from_env(),
)

result = Runner.run_sync(
    agent,
    "Tell a joke only Python developers would enjoy.",
)
print(result.output)
```

Use `Runner.run_sync()` in a normal script. In async code, use
`await Runner.run(...)`; both execute the same run loop.

## 3. Add a Tool

`@tool` turns an ordinary typed function into a capability the model can call.
The function signature becomes JSON Schema, and the docstring tells the model
when to use it.

```python
from lovia import Agent, Runner, model_from_env, tool


@tool
def check_inventory(sku: str) -> str:
    """Look up the stock level for a product SKU."""
    return f"{sku}: 41 units in stock"


agent = Agent(
    name="shop-assistant",
    instructions="Use tools for factual inventory questions.",
    model=model_from_env(),
    tools=[check_inventory],
)

result = Runner.run_sync(
    agent,
    "Do you have SKU-1401 in stock? Add one buying tip.",
)
print(result.output)
print(f"turns={result.turns}, tokens={result.usage.total_tokens}")
```

If the model calls `check_inventory`, the first Turn contains the model reply
and tool execution; a second Turn lets the model answer using the result. See
[Core concepts](concepts.md#run-vs-turn) for the distinction.

## Choose your next step

| Goal | Guide |
| --- | --- |
| Stream tokens and tool events | [Streaming](streaming.md) · [`03_streaming.py`](../../examples/03_streaming.py) |
| Return a validated object | [Structured output](structured-output.md) · [`04_structured_output.py`](../../examples/04_structured_output.py) |
| Persist a conversation | [Sessions & checkpoints](sessions-and-checkpoints.md) · [`05_sessions.py`](../../examples/05_sessions.py) |
| Give the Agent files and a shell | [Workspace](workspace.md) · [`20_workspace_agent.py`](../../examples/20_workspace_agent.py) |
| Open a chat UI | [Web UI & server](web.md) · [`26_web_serve.py`](../../examples/26_web_serve.py) |
| Browse the complete learning path | [Runnable examples](../../examples/README.md) |
