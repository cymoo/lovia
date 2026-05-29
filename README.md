# lovia

A lightweight, provider-neutral agent framework for Python.

[简体中文](./README-zh.md)

```python
import asyncio
from lovia import Agent, Runner

agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant.",
    model="openai:gpt-4o-mini",
)
result = asyncio.run(Runner.run(agent, "What is the capital of France?"))
print(result.output)  # Paris
```

**Two hard dependencies** (`httpx`, `pydantic`). No DSL, no graph, no global state.
Every advanced feature — tools, sessions, handoffs, structured output, MCP, streaming — is opt-in.

---

## Install

```bash
pip install lovia
```

Optional extras:

```bash
pip install "lovia[mcp]"    # Model Context Protocol client
pip install "lovia[tools]"  # web_search with DuckDuckGo backend
pip install "lovia[web]"    # FastAPI + SSE chat server
```

---

## Tools

Any typed Python function becomes a tool with `@tool`. Sync and async both work.

```python
from lovia import Agent, Runner, tool

@tool
def calculate(expression: str) -> float:
    """Evaluate a simple math expression."""
    return eval(expression, {"__builtins__": {}})

agent = Agent(
    name="Calc",
    instructions="Use calculate() for arithmetic.",
    model="openai:gpt-4o-mini",
    tools=[calculate],
)
result = asyncio.run(Runner.run(agent, "What is 1337 * 42?"))
```

Use `Annotated` to add per-parameter descriptions to the JSON schema:

```python
from typing import Annotated

@tool
def search(
    query: Annotated[str, "Keywords to search for."],
    limit: Annotated[int, "Max results, 1-20."] = 5,
) -> list[str]: ...
```

Simple execution policies stay as decorator kwargs:

```python
@tool(timeout=5, retries=2, needs_approval=True)
async def send_email(to: str, body: str) -> str: ...
```

For advanced cases, pass composable `policies`; simple kwargs still work.

```python
from lovia import RunContext

async def redact(next_tool, args, ctx):
    result = await next_tool(args, ctx)
    return str(result).replace(ctx.context.api_key, "[redacted]")

@tool(policies=[redact])
async def call_api(ctx: RunContext, path: str) -> str: ...
```

---

## Structured output

Pass any Pydantic model as `output_type` and the result is validated automatically.
`output_repair=True` lets the model self-correct if the first parse fails.

```python
from pydantic import BaseModel
from lovia import Agent, Runner

class Review(BaseModel):
    rating: int       # 1-5
    summary: str
    pros: list[str]
    cons: list[str]

agent = Agent(
    name="Reviewer",
    instructions="Extract a structured review from the user text.",
    model="openai:gpt-4o-mini",
    output_type=Review,
    output_repair=True,
)
result = asyncio.run(Runner.run(agent, "The battery lasts all day but the screen is dim."))
print(result.output.rating)   # -> int
```

Override `output_type` for a single call without touching the agent:

```python
result = await Runner.run(agent, "Summarize in plain text.", output_type=str)
```

---

## Streaming

```python
async for event in Runner.stream(agent, "Tell me a joke"):
    print(event)
```

Or directly from the agent instance:

```python
async for event in agent.stream("Tell me a joke"):
    print(event)
```

---

## Dynamic instructions

Inject context-aware content at runtime with `@agent.system_prompt`.
Multiple fragments compose with the base `instructions`.

```python
agent = Agent(name="Support", instructions="You are a support bot.", model="openai:gpt-4o-mini")

@agent.system_prompt
async def inject_user(ctx) -> str:
    user = await db.get_user(ctx.context.user_id)
    return f"The user's name is {user.name}. Their plan is {user.plan}."

# Append one-off context at call time:
result = await Runner.run(agent, "I need help.", append_instructions="Reply in Spanish.")
```

Prefer functional configuration when cloning reusable agents:

```python
agent = agent.with_system_prompt(inject_user)
```

---

## Handoffs

An agent can delegate to another agent mid-conversation.
The Runner follows the chain automatically.

```python
billing = Agent(name="Billing", instructions="Handle billing questions.", model="openai:gpt-4o-mini")
support = Agent(name="Support", instructions="Answer support questions. Hand off billing questions.", model="openai:gpt-4o-mini", handoffs=[billing])

result = await Runner.run(support, "Can I get a refund?")
```

---

## Sessions

Persist conversation history across calls with a `session=` argument.
The default in-memory store is a good starting point; swap in Redis or SQL as needed.

```python
from lovia.stores import InMemorySessionStore

session_store = InMemorySessionStore()

result1 = await Runner.run(agent, "My name is Alice.", session=session_store.session("u42"))
result2 = await Runner.run(agent, "What is my name?", session=session_store.session("u42"))
# → "Your name is Alice."
```

---

## Approval (human in the loop)

Mark sensitive tools with `needs_approval=True` to require human sign-off.

```python
from lovia import ApprovalChannel

channel = ApprovalChannel()

@tool(needs_approval=True)
def send_email(to: str, body: str) -> str:
    ...

# In your UI, call channel.approve(request_id) or channel.deny(request_id, reason)
result = await Runner.run(agent, "Send a welcome email to alice@example.com", approval_channel=channel)
```

---

## Sync helpers

`Runner.run_sync` and `agent.run_sync` are convenience wrappers around
`asyncio.run`. Use them in scripts or wherever you can't `await`.

```python
result = Runner.run_sync(agent, "What is 2+2?")
print(result.output)
```

---

## Tools

`lovia.tools` ships practical, framework-agnostic tools you can drop straight into any agent.
Nothing is imported automatically — grab only what you need.

```python
from lovia.tools.http import http_fetch
from lovia.tools.search import duckduckgo_search_tool
from lovia.tools.todo import TodoList, todo_tools
from lovia.tools.human import HumanChannel, ask_human
from lovia.tools.think import think
from lovia.tools.time import now

todos = TodoList()
channel = HumanChannel()

agent = Agent(
    name="Worker",
    instructions="Plan, reason, act.",
    model="openai:gpt-4o-mini",
    tools=[
        http_fetch, now, think,
        duckduckgo_search_tool(),  # requires lovia[tools]
        *todo_tools(todos),
        ask_human(channel),
    ],
)
```

Tool convention: stateless helpers export ready-to-use `Tool` instances,
pluggable backends use factories, stateful single-tool helpers expose `.tool()`,
and stateful multi-tool helpers expose `.tools()`.

Filesystem and shell tools use the same `lovia.tools` namespace, either through
`Agent(sandbox=...)` or direct factories like `coding_tools(root=".")`.

Runnable demos live in [`examples/tools/`](./examples/tools/).

---

## Sandbox tools

`lovia.sandbox` is the concise filesystem + process layer for coding agents.
Give the agent a sandbox once and Lovia injects the standard file and shell
tools for that boundary.

```python
from lovia import Agent, Runner
from lovia.sandbox import Sandbox

agent = Agent(
    name="coder",
    instructions="You are a focused coding agent.",
    model="openai:gpt-4o-mini",
    sandbox=Sandbox.local("."),
)

await Runner.run(agent, "Create app.py and run it.")
```

If you are already running inside Docker or another controlled environment,
use the same tool implementations directly:

```python
from lovia.tools import coding_tools

agent = Agent(
    name="coder",
    model="openai:gpt-4o-mini",
    tools=coding_tools(root="."),
)
```

What you get for free:

* **Path traversal guard** — symlink-aware, blocks `..`, `/etc/...`, etc.
  File tools accept sandbox-relative paths only.
* **Simple atomic tools** — `read_file`, `write_file`, `edit_file`,
  `glob`, `list_dir`, and `shell` are implemented once and can be used through
  either `Agent(sandbox=...)` or `coding_tools(root=...)`.
* **Exact edits** — `edit_file` replaces exact `old_text` with `new_text`;
  if the text is missing or ambiguous, it fails without writing so the
  model can re-read and retry.
* **Structured command results** — `shell` returns `exit_code`, `stdout`,
  `stderr`, `timed_out`, and `truncated`.
* **Approval-aware shell** — `mode="coding"` allows file edits but routes
  shell commands through Lovia's existing approval flow. Use `mode="trusted"`
  for fully automated local runs.
* **Hidden-file filtering** — `glob` and `list_dir` skip dotfiles by default.
  Pass `include_hidden=True` to look.

`Sandbox.local(".")` is **not a security boundary**. It confines Lovia's file
APIs to a root and gates shell by policy, but approved commands run as the host
user and writes modify real files. Future Docker / remote implementations can
plug in by implementing the `SandboxBackend` Protocol.

---

## Web UI

`lovia.web` ships a small FastAPI app + bundled vanilla-JS chat UI. It's
the same wiring you'd build yourself, but pre-assembled so you can ship a
demo in three lines:

```python
from lovia import Agent
from lovia.web import serve

agent = Agent(name="assistant", instructions="…", model="openai:gpt-4o-mini")
serve(agent, db_path="lovia.db")   # http://127.0.0.1:8000
```

What you get out of the box:

* **Sidebar of chats** — every session lives in SQLite (`db_path`), so it
  survives restarts, can be renamed, deleted, switched.
* **Auto-generated titles** — after the first turn a tiny background
  call asks the same model for a 3-6 word headline.
* **Streaming transcript** with tool-call cards and approval prompts.
* **Tool-call cards and approval prompts** — UI primitives for real agent
  work, without coupling the core framework to a particular sandbox backend.

---

## Skills

Skills are Markdown-driven instruction packs stored in a directory tree.
They let you compose domain knowledge without bloating the system prompt.

```
skills/
  translation/
    SKILL.md          # name, description, usage instructions
    references/       # reference files the agent can read
```

```python
from lovia.skills import SkillCatalog

catalog = SkillCatalog.from_dir("./skills")   # lazy by default
agent = Agent(
    name="Expert",
    instructions=catalog.render_catalog(),
    model="openai:gpt-4o-mini",
    tools=catalog.tools(),
)
```

In lazy mode the catalog renders as a compact index; the model calls
`load_skill` to pull in a full skill body on demand. Switch to
`mode="eager"` to inline all bodies up front.

---

## Multiple providers

The `model=` field accepts any `"provider:model"` string or a `Provider` instance.

```python
# OpenAI
agent = Agent(model="openai:gpt-4o-mini", ...)
# Anthropic
agent = Agent(model="anthropic:claude-3-5-haiku-20241022", ...)
# Any OpenAI-compatible endpoint
from lovia import OpenAIChatProvider
provider = OpenAIChatProvider(model="deepseek-chat", base_url="https://api.deepseek.com/v1", api_key="...")
agent = Agent(model=provider, ...)
```

---

## Examples

```
examples/
  01_hello.py                  Minimal agent
  02_tools.py                  Tool calling
  03_streaming.py              Streaming tokens
  04_structured_output.py      Pydantic output
  05_handoff.py                Agent-to-agent delegation
  06_agent_as_tool.py          Sub-agent as a tool
  07_session.py                Persistent sessions
  08_skills.py                 SkillCatalog
  09_compat_provider.py        Custom OpenAI-compatible provider
  10_hooks.py                  Lifecycle hooks / tracing
  11_approval.py               Human-in-the-loop approval
  12_multimodal.py             Image input
  13_budget_and_cancel.py      Token budget & cancellation
  14_guardrails.py             Input/output guards
  15_resume.py                 Resume interrupted runs
  16_web_serve.py              FastAPI + SSE server
  17_responses_reasoning.py    OpenAI Responses API + reasoning
  18_context_policy.py         Auto-summarize long history
  19_dynamic_instructions.py   Dynamic system prompt
  20_builtins.py               Several built-in tools together
  21_dx.py                     Annotated schemas, run_sync
  22_sandbox.py                Local Sandbox + code tools
  23_sandbox_agent.py          Coding agent with Sandbox.local
  tools/                       One focused demo per tool
  workflows/                   Multi-agent workflow patterns
```

---

## Development

```bash
git clone https://github.com/cymoo/lovia
pip install -e ".[dev]"
pytest          # run tests
ruff check .    # lint
mypy lovia      # type-check
```

See [`AGENTS.md`](./AGENTS.md) for architecture notes, design philosophy,
and commit conventions.

---

MIT License
