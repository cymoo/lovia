# lovia

A lightweight, async-first agent framework for Python.  
Core has exactly two dependencies: `httpx` and `pydantic`.

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
        instructions="Answer briefly. Use tools when needed.",
        model="openai:deepseek-v4-pro",
        tools=[add],
    )
    result = await Runner.run(agent, "What is 2 + 3?")
    print(result.output)  # 5


asyncio.run(main())
```

---

## Agent

`Agent` is a plain dataclass — no inheritance required:

```python
from lovia import Agent

agent = Agent(
    name="writer",
    instructions="Write concise, concrete answers.",
    model="openai:deepseek-v4-pro",
)
```

Dynamic system-prompt fragments can be added at run time:

```python
@agent.system_prompt
async def inject_user_tier(ctx) -> str:
    return f"User tier: {ctx.context['tier']}"
```

Need a variant? Clone without touching the original:

```python
strict = agent.clone(instructions="Always cite sources.", output_type=Report)
```

## Running agents

```python
from lovia import Runner

result = await Runner.run(agent, "Draft a release note.")
print(result.output)
```

Streaming delivers typed events:

```python
from lovia import events

handle = Runner.stream(agent, "Tell me a short story.")
async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

Sync wrapper for scripts:

```python
result = Runner.run_sync(agent, "Summarize this.")
```

## Tools

Any typed Python function becomes a tool:

```python
from typing import Annotated
from pydantic import Field
from lovia import tool


@tool
async def fetch_weather(city: str) -> str:
    """Get current weather for a city."""
    ...


@tool(strict=True)
async def search_docs(
    query: Annotated[str, Field(description="Search terms")],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """Search internal documentation."""
    ...
```

lovia generates JSON Schema from type hints, docstrings, and `Annotated`/`Field` metadata.

### Tool approval

Sensitive tools can require explicit approval before they run:

```python
@tool(needs_approval=True)
async def delete_record(record_id: str) -> str:
    """Permanently delete a record."""
    ...
```

Programmatic approval (e.g. for automated pipelines):

```python
agent = Agent(
    ...,
    approval_handler=lambda call, ctx: call.name != "delete_record",
)
```

In streaming mode the runner emits `ApprovalRequired`; your UI or CLI
resolves it:

```python
async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()   # or ev.deny("not allowed")
```

## Multi-agent: handoff and composition

### Handoff

The triage agent routes requests to specialist agents:

```python
from lovia import Agent
from lovia.handoff import Handoff

billing = Agent(name="billing", instructions="Handle billing questions.", model="openai:deepseek-v4-pro")
support = Agent(name="support", instructions="Handle technical issues.", model="openai:deepseek-v4-pro")

triage = Agent(
    name="triage",
    instructions="Route to the right specialist.",
    model="openai:deepseek-v4-pro",
    handoffs=[billing, support],
)

result = await Runner.run(triage, "I was charged twice.")
```

On handoff, conversation history is shared. Use `input_filter` to strip
stale tool calls before the new agent sees the transcript:

```python
from lovia.handoff import Handoff, drop_stale_tool_calls

Handoff(target=billing, input_filter=drop_stale_tool_calls)
```

### Agent as tool

Wrap an agent so a parent can delegate sub-tasks to it:

```python
summarizer = Agent(name="summarizer", instructions="Summarize text.", model="openai:deepseek-v4-pro")

orchestrator = Agent(
    name="orchestrator",
    model="openai:deepseek-v4-pro",
    tools=[summarizer.as_tool(description="Summarize a passage of text.")],
)
```

The sub-agent runs in an isolated loop; its output is returned as the tool result.

## Human in the loop

### Approval gates

Set `needs_approval=True` on any tool. The runner pauses until the call
is approved or denied — by your streaming consumer, a web handler, or the
`approval_handler` on the agent.

### Asking the human a question

`ask_human` lets the model explicitly request input from an operator:

```python
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()
agent = Agent(
    name="assistant",
    model="openai:deepseek-v4-pro",
    tools=[ask_human(channel)],
)

# In your driver / UI:
async def ui_loop():
    handle = Runner.stream(agent, "I need clarification.")
    async for ev in handle:
        ...

# Elsewhere in your event loop — resolve pending questions:
for q in channel.pending:
    channel.answer(q.id, "Please proceed with option A.")
```

## Hooks

`AgentHooks` is a subscriber that fires on lifecycle events:

```python
from lovia.hooks import AgentHooks
from lovia import events

hooks = AgentHooks()

@hooks.on(events.ToolCallStarted)
async def log_tool(ev):
    print(f"→ {ev.call.name}({ev.call.arguments})")

@hooks.on((events.RunCompleted, events.ErrorOccurred))
def at_end(ev):
    print("done:", type(ev).__name__)

agent = Agent(..., hooks=hooks)
```

Handlers may be sync or async; both are supported.

## Guardrails

Async callables that veto a run before it starts (input) or after it
finishes (output):

```python
from lovia.exceptions import GuardrailTripped


async def no_pii(messages, ctx):
    for m in messages:
        if "@" in str(m.content):
            raise GuardrailTripped("PII detected — email address in input.")


async def must_cite(output, ctx):
    if "source:" not in output.lower():
        return "Response must include a source citation."  # truthy = violation


agent = Agent(
    name="researcher",
    model="openai:deepseek-v4-pro",
    input_guardrails=[no_pii],
    output_guardrails=[must_cite],
)
```

Returning `None` (or `False`) means the check passed.

## Structured output

Pass any Pydantic model to get validated, typed output:

```python
from pydantic import BaseModel


class Summary(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(
    name="summarizer",
    model="openai:deepseek-v4-pro",
    output_type=Summary,
)
result = await Runner.run(agent, "Summarize lovia in three bullets.")
print(result.output.title)
```

Override the type per call without changing the agent:

```python
result = await Runner.run(agent, "Give me a JSON summary.", output_type=Summary)
```

## Sessions and long conversations

Persist transcript state across multiple calls:

```python
from lovia.stores import SQLiteSession

session = SQLiteSession("chat.db")
await Runner.run(agent, "My project is called Atlas.", session=session, session_id="u1")
await Runner.run(agent, "What is my project called?",  session=session, session_id="u1")
```

For long-running chats, a context policy trims old messages before the
model's window fills up:

```python
from lovia import SummarizingContextPolicy

policy = SummarizingContextPolicy(keep_recent_messages=10)
result = await Runner.run(agent, "Continue.", context_policy=policy)
```

## Skills

Skills are file-backed prompt fragments loaded on demand — good for large
domain knowledge that shouldn't always occupy the context window:

```python
from lovia.skills import SkillCatalog

catalog = SkillCatalog("skills/", mode="lazy")   # or mode="eager"

agent = Agent(
    name="support",
    model="openai:deepseek-v4-pro",
    skills=catalog,
)
```

Each skill is a directory with a `SKILL.md` (YAML frontmatter + body).
In lazy mode, the model calls `load_skill(name)` when needed; in eager
mode all skill bodies are inlined at startup.

## Built-in tools

Practical tools live under `lovia.tools` — nothing is imported automatically:

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
    model="openai:deepseek-v4-pro",
    tools=[
        http_fetch,
        duckduckgo_search_tool(),
        *todo_tools(todos),
        now,
        think,
    ],
)
```

Focused examples are in [`examples/tools/`](./examples/tools/).

## Sandbox and coding agent

For a coding agent, attach a sandbox instead of manually wiring each tool:

```python
from lovia import Agent
from lovia.sandbox import Sandbox

agent = Agent(
    name="coder",
    instructions="Make small, targeted edits.",
    model="openai:deepseek-v4-pro",
    sandbox=Sandbox.local(".", mode="coding"),
)
```

| Mode | Tools exposed |
| --- | --- |
| `"readonly"` | read_file, list_dir, glob |
| `"coding"` | read_file, write_file, edit_file, list_dir, glob + shell (approval required) |
| `"trusted"` | all of the above, shell without approval |

Local sandbox paths are root-relative. Absolute paths, `..` escapes, and
symlink escapes are rejected. The local shell still runs as the host user
— this is a convenience boundary, not a hard security sandbox.

You can also use the tool factories directly:

```python
from lovia.tools import coding_tools

agent = Agent(
    name="coder",
    model="openai:deepseek-v4-pro",
    tools=coding_tools(root=".", mode="coding"),
)
```

## Web UI

A minimal FastAPI app with streaming, sessions, markdown rendering, and
approval support:

```bash
pip install "lovia[web]"
python examples/16_web_serve.py
```

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

Features: SSE streaming · persistent sessions · tool approval via HTTP ·
safe markdown rendering · Jinja2-rendered no-build UI.

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

## Install extras

| Need | Install |
| --- | --- |
| Core | `pip install lovia` |
| DuckDuckGo search | `pip install "lovia[tools]"` |
| MCP integration | `pip install "lovia[mcp]"` |
| Web UI | `pip install "lovia[web]"` |
| Prefect workflows | `pip install "lovia[prefect]"` |
| Run all examples | `pip install "lovia[examples,web]"` |
| Dev / CI | `pip install -e ".[dev]"` |

