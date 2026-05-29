# lovia

lovia is a lightweight async agent framework for Python. It keeps the core
small, lets you bring your own model provider, and adds practical opt-in layers
for tools, sandboxes, sessions, workflows, and a tiny web UI.

```bash
pip install lovia
```

```python
import asyncio
from lovia import Agent, Runner, tool


@tool
async def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


async def main() -> None:
    agent = Agent(
        name="calc",
        instructions="Answer briefly. Use tools when useful.",
        model="openai:gpt-4o-mini",
        tools=[add],
    )
    result = await Runner.run(agent, "What is 2 + 3?")
    print(result.output)


asyncio.run(main())
```

## Why lovia?

- **Small core**: only `httpx` and `pydantic` are hard dependencies.
- **Async-first**: public APIs are async; sync helpers are convenience wrappers.
- **Provider-neutral**: use OpenAI-compatible chat, OpenAI Responses, or your own provider.
- **Tools without ceremony**: turn a typed Python function into a model tool with `@tool`.
- **Sandboxed coding tools**: attach file and shell tools with a clear root boundary.
- **Optional layers**: web UI, MCP, search, Rich examples, and Prefect workflows stay in extras.

## Install extras

| Need | Install |
| --- | --- |
| Core agent framework | `pip install lovia` |
| `.env` loading in examples | `pip install "lovia[dotenv]"` |
| DuckDuckGo search tool | `pip install "lovia[tools]"` |
| MCP integration | `pip install "lovia[mcp]"` |
| Web UI | `pip install "lovia[web]"` |
| Rich terminal examples | `pip install "lovia[rich]"` |
| Prefect workflow example | `pip install "lovia[prefect]"` |
| Everything used by examples | `pip install "lovia[examples,web]"` |
| Development | `pip install -e ".[dev]"` |

## Core concepts

### Agent

`Agent` is a dataclass that describes the assistant: name, instructions, model,
tools, output type, hooks, sandbox, and runtime policy.

```python
agent = Agent(
    name="writer",
    instructions="Write concise, concrete answers.",
    model="openai:gpt-4o-mini",
)
```

### Runner

`Runner` executes the loop: send messages to the model, run requested tools,
append results, and stop when the model returns a final answer.

```python
result = await Runner.run(agent, "Draft a release note.")
print(result.output)
```

Streaming gives you typed events:

```python
from lovia import events

handle = Runner.stream(agent, "Tell me a short story.")
async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

### Tool

Any typed Python callable can become a tool. lovia generates JSON Schema from
type hints and docstrings.

```python
from typing import Annotated
from pydantic import Field
from lovia import tool


@tool(strict=True)
async def search_docs(
    query: Annotated[str, Field(description="What to search for")],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """Search internal docs."""
    return [f"result for {query}"]
```

Use `needs_approval=True` for sensitive tools. In streaming mode, the runner
emits `ApprovalRequired`; your UI or CLI can approve or deny it.

## Built-in tools

Practical tools live under `lovia.tools`. Nothing is imported automatically.

```python
from lovia.tools.http import http_fetch
from lovia.tools.search import duckduckgo_search_tool
from lovia.tools.todo import TodoList, todo_tools
from lovia.tools.human import HumanChannel, ask_human
from lovia.tools.time import now
from lovia.tools.think import think

todos = TodoList()
agent = Agent(
    name="assistant",
    model="openai:gpt-4o-mini",
    tools=[
        http_fetch,
        duckduckgo_search_tool(),
        *todo_tools(todos),
        now,
        think,
    ],
)
```

Focused examples live in [`examples/tools/`](./examples/tools/).

## Sandbox and coding tools

For coding agents, attach a sandbox instead of manually wiring each file/shell
tool:

```python
from lovia import Agent
from lovia.sandbox import Sandbox

agent = Agent(
    name="coder",
    instructions="Make small, safe edits.",
    model="openai:gpt-4o-mini",
    sandbox=Sandbox.local(".", mode="coding"),
)
```

`mode="coding"` exposes read/write/edit/list/glob plus shell with approval.
`mode="readonly"` exposes only read/list/glob. `mode="trusted"` allows shell
without approval.

You can also use the factories directly:

```python
from lovia.tools import coding_tools

agent = Agent(
    name="coder",
    model="openai:gpt-4o-mini",
    tools=coding_tools(root=".", mode="coding"),
)
```

Local sandbox paths are root-relative and reject absolute paths, `..` escapes,
and symlink escapes. Local shell commands still run as the host user; the local
sandbox is a convenience boundary, not a hard security boundary.

## Structured output

Pass a Pydantic model to get validated output:

```python
from pydantic import BaseModel


class Summary(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(
    name="summarizer",
    model="openai:gpt-4o-mini",
    output_type=Summary,
)
result = await Runner.run(agent, "Summarize lovia.")
print(result.output.title)
```

You can override the output type per call:

```python
result = await Runner.run(agent, "Return JSON summary.", output_type=Summary)
```

## Sessions and long conversations

Use sessions to persist transcript state:

```python
from lovia.stores import SQLiteSession

session = SQLiteSession("chat.db")
await Runner.run(agent, "Remember my project is named Atlas.", session=session, session_id="u1")
await Runner.run(agent, "What is my project called?", session=session, session_id="u1")
```

For long-running chats, add a context policy:

```python
from lovia import SummarizingContextPolicy

policy = SummarizingContextPolicy(keep_recent_messages=10)
result = await Runner.run(agent, "continue", context_policy=policy)
```

## Web UI

The optional web layer gives you a small FastAPI app with:

- streamed assistant responses via SSE;
- persistent chat sessions when you pass `db_path`;
- HTTP approval for sensitive tools;
- markdown rendering for assistant messages;
- a Jinja2-rendered, no-build chat page.

```bash
pip install "lovia[web,dotenv]"
python examples/16_web_serve.py
```

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

## Prefect workflows

lovia works well inside workflow orchestrators. The Prefect example wraps an
agent call in a retryable task and calls it from a flow:

```bash
pip install "lovia[examples]"
python examples/24_prefect.py
```

```python
from prefect import flow, task
from lovia import Agent, Runner


@task(retries=1)
async def ask_agent(topic: str) -> str:
    result = await Runner.run(Agent(name="planner", model="openai:gpt-4o-mini"), topic)
    return str(result.output)


@flow
async def plan() -> str:
    return await ask_agent("plan a small release")
```

## Examples

| File | What it shows |
| --- | --- |
| `examples/01_hello.py` | minimal agent run |
| `examples/02_tools.py` | custom `@tool` |
| `examples/03_streaming.py` | streaming output with Rich |
| `examples/04_structured_output.py` | Pydantic output |
| `examples/05_handoff.py` | agent handoff |
| `examples/08_skills.py` | skill catalog |
| `examples/11_approval.py` | tool approval |
| `examples/16_web_serve.py` | web UI |
| `examples/22_sandbox.py` | direct sandbox session |
| `examples/23_sandbox_agent.py` | coding agent with sandbox |
| `examples/24_prefect.py` | Prefect flow integration |
| `examples/tools/` | focused tool demos |
| `examples/workflows/` | workflow patterns |

## Development

```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy lovia
pytest -q
```

Core stays intentionally small. New integrations should normally be optional
extras, examples, or user-side recipes unless they simplify the framework
without adding weight.
