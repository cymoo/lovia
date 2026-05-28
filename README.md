# lovia

A lightweight, provider-neutral agent framework for Python.

[简体中文](./README-zh.md)

```python
from lovia import Agent, Runner

agent = Agent(name="Greeter", instructions="Reply in one short line.", model="openai:gpt-4o-mini")
print((await Runner.run(agent, "Say hi in three languages.")).output)
```

- **No DSL, no graph, no implicit globals** — plain Python with type hints.
- **Two deps in core** — `httpx` and `pydantic`. Everything else is opt-in.
- **Async-first**, with `run_sync` helpers where they pay for themselves.
- **Provider-neutral** — OpenAI Chat & Responses, Anthropic, anything OpenAI-compatible.

---

## Install

```bash
pip install lovia                 # core
pip install "lovia[mcp]"          # + Model Context Protocol client
pip install "lovia[tools]"        # + DuckDuckGo backend for web_search
pip install "lovia[web]"          # + FastAPI + SSE + bundled chat UI
pip install "lovia[dev]"          # + pytest, ruff, mypy
```

Python 3.10+.

---

## Quickstart

```python
import asyncio
from lovia import Agent, Runner, tool

@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

agent = Agent(
    name="Calc",
    instructions="Use tools when you need to compute.",
    model="openai:gpt-4o-mini",
    tools=[add],
)

print(asyncio.run(Runner.run(agent, "What is 17 + 25?")).output)
```

Sync entry-point (handy in scripts and notebooks):

```python
result = Runner.run_sync(agent, "What is 17 + 25?")
# Or, equivalently, from the agent itself:
result = agent.run_sync("What is 17 + 25?")
```

Stream events as they arrive:

```python
async for event in agent.stream("Tell me a joke"):
    print(event)
```

---

## Core concepts

### Agent

```python
agent = Agent(
    name="Concierge",
    instructions="Be terse.",       # str or (ctx) -> str | Awaitable[str]
    model="openai:gpt-4o-mini",     # "provider:model" or any Provider instance
    tools=[...],
    output_type=MyPydanticModel,    # optional — anything Pydantic can validate
    handoffs=[other_agent],         # delegate to other agents
)
```

### Dynamic instructions

Append fragments at config time with `@agent.system_prompt`, or at call
time via `append_instructions=`. Both compose cleanly with the static
`instructions=` base.

```python
agent = Agent(name="Helper", instructions="You are a helpful assistant.")

@agent.system_prompt
def add_user(ctx) -> str:
    user = getattr(ctx.context, "user", None)
    return f"The user's name is {user}." if user else ""

await Runner.run(agent, "Hi", append_instructions="Reply in haiku.")
```

### Per-call `output_type` override

The agent's declared `output_type` is the default — `Runner.run` (and
`agent.run`) can override it on a single call. Pass `None` to reset to
plain text.

```python
class Plan(BaseModel):
    steps: list[str]

agent = Agent(name="x", instructions="...", output_type=Plan)
plan = (await Runner.run(agent, "Plan a trip")).output           # -> Plan
text = (await Runner.run(agent, "Plan a trip", output_type=None)).output  # -> str
```

### Tools

`@tool` turns a typed Python function into a tool. Use `Annotated[..., "desc"]`
or `Annotated[..., Field(description=...)]` to enrich the JSON Schema. Pass
`strict=True` for OpenAI strict-mode schemas.

```python
from typing import Annotated
from lovia import tool

@tool(strict=True)
def search(
    query: Annotated[str, "Search query."],
    limit: Annotated[int, "Max results."] = 5,
) -> list[str]: ...
```

### Friendly errors

Every framework exception carries an optional `.hint`. `OutputValidationError`
adds the raw model text and the failing schema name to make debugging fast:

```
OutputValidationError: 2 validation errors for Plan
hint: Consider setting output_repair=True on Runner.run().
raw : '{"steps": "buy ticket"}'
```

---

## Builtin tools (opt-in)

Everything below lives under `lovia.builtins.*`. Nothing is imported from
the top-level package automatically.

| Module | What you get |
| --- | --- |
| `lovia.builtins.http`   | `http_fetch` — typed wrapper around `httpx` |
| `lovia.builtins.time`   | `now`, `sleep` |
| `lovia.builtins.think`  | `think` — scratchpad |
| `lovia.builtins.fs`     | `FileSystem(root, writable=False)` — sandboxed `read_file`/`write_file`/`list_dir`/`glob` |
| `lovia.builtins.shell`  | `Shell(cwd, needs_approval=True)` (+ `allowlist`) |
| `lovia.builtins.code`   | `PythonRunner(needs_approval=True)` |
| `lovia.builtins.search` | `web_search(impl=None)` + `WebSearch` Protocol + `DuckDuckGoSearch` |
| `lovia.builtins.todo`   | `TodoList` + `todo_tools(state)` |
| `lovia.builtins.human`  | `HumanChannel` + `ask_human(channel)` |

Runnable examples for each live in [`examples/builtins/`](./examples/builtins/).

---

## Structured output

```python
from pydantic import BaseModel
class Answer(BaseModel):
    summary: str
    confidence: float

agent = Agent(name="x", instructions="...", output_type=Answer, output_repair=True)
```

`output_repair=True` asks the model to fix its own JSON if the first
parse fails — usually one extra round-trip away from green.

---

## Skills (`SkillCatalog`)

Skills are Markdown-driven instruction packs with optional `references/`,
`scripts/`, and `assets/` subdirectories. Modes:

- **lazy** (default) — render only the index; the model loads bodies on demand via `load_skill`.
- **eager** — inline every `SKILL.md` body into the system prompt.

```python
from lovia.skills import SkillCatalog

catalog = SkillCatalog.from_dir("./skills")          # mode="lazy"
agent = Agent(
    name="Researcher",
    instructions="...",
    tools=catalog.tools(),
)
prompt = catalog.render_catalog()
```

See `examples/08_skills.py`.

---

## Examples

| File | Highlights |
| --- | --- |
| `01_minimal.py`            | Hello world |
| `02_tools.py`              | `@tool` basics |
| `03_structured_output.py`  | Pydantic outputs |
| `04_streaming.py`          | Token streaming |
| `05_handoff.py`            | Agent → agent |
| `06_guardrails.py`         | Input / output guards |
| `07_approval.py`           | Human-in-the-loop |
| `08_skills.py`             | SkillCatalog |
| `09_memory.py`             | Persistent context |
| `10_sessions.py`           | Pluggable session stores |
| `11_mcp.py`                | Model Context Protocol |
| `12_tracing.py`            | Hooks & tracing |
| `13_anthropic.py`          | Anthropic provider |
| `14_provider_swap.py`      | Swap providers per call |
| `15_context_policy.py`     | Auto-summarize long history |
| `16_web.py`                | FastAPI + SSE chat UI |
| `17_dynamic_provider.py`   | Route per-message |
| `18_hooks.py`              | Lifecycle hooks |
| `19_dynamic_instructions.py` | `@agent.system_prompt` + `append_instructions=` |
| `20_builtins.py`           | A few `lovia.builtins.*` together |
| `21_dx.py`                 | `Annotated`/`Field`, `run_sync`, `Agent.run` |
| `builtins/`                | One demo per builtin tool |

---

## Development

```bash
pip install -e .[dev]
pytest               # tests
ruff check .         # lint
ruff format .        # format
mypy lovia           # type-check
```

See [`AGENTS.md`](./AGENTS.md) for design philosophy and contribution conventions.

## License

MIT.
