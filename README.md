# lovia

A Python agent framework that stays out of your way.

```bash
pip install lovia
```

```python
# Set once in your environment (or .env):
# OPENAI_BASE_URL=https://api.deepseek.com
# OPENAI_API_KEY=sk-your-key

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
        model="deepseek-v4-pro",
        tools=[add],
    )
    result = await Runner.run(agent, "What is 2 + 3?")
    print(result.output)  # 5


asyncio.run(main())
```

---

## Why lovia?

The LLM agent space is crowded. lovia makes a specific set of trade-offs:

- 🪶 **Minimal concepts** — Agent, Runner, tool. The whole mental model fits on one page.
- 🔌 **Provider-neutral** — OpenAI, Anthropic, any OpenAI-compatible endpoint. Swap with one line.
- 🧩 **Extend without subclassing** — Protocols and dataclasses throughout. Plug in your own session store, memory backend, or provider without touching framework internals.
- ✂️ **Thin by default** — Only `httpx` and `pydantic` are required. Web UI, MCP, search, and orchestration stay optional.
- 🛡️ **Production primitives** — Guardrails, approval gates, lifecycle hooks, policy-scoped workspace tools, and pluggable features (todo checklists, your own) — available when you need them, invisible when you don't.

---

## Agent

`Agent` is a plain dataclass — no inheritance required:

```python
from lovia import Agent

agent = Agent(
    name="writer",
    instructions="Write concise, concrete answers.",
    model="deepseek-v4-pro",
)
```

Dynamic system-prompt fragments can be injected at run time:

```python
@agent.system_prompt
async def add_context(ctx) -> str:
    return f"User tier: {ctx.context['tier']}"
```

Need a one-off variant? Clone without mutating the original:

```python
strict = agent.clone(instructions="Always cite sources.", output_type=Report)
```

## Runner

```python
from lovia import Runner

result = await Runner.run(agent, "Draft a release note.")
print(result.output)
```

Streaming delivers typed events as they arrive:

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

Any typed Python function becomes a tool. lovia generates JSON Schema from
type hints, docstrings, and `Annotated`/`Field` metadata automatically:

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

### Tool approval

Flag sensitive tools to require explicit sign-off before they run:

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

In streaming mode the runner emits `ApprovalRequired`; your UI resolves it:

```python
async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()   # or ev.deny("reason")
```

## Structured output

Pass a Pydantic model to get validated, typed output:

```python
from pydantic import BaseModel


class Summary(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(
    name="summarizer",
    model="deepseek-v4-pro",
    output_type=Summary,
)
result = await Runner.run(agent, "Summarize lovia in three bullets.")
print(result.output.title)
```

Override the type per call without changing the agent:

```python
result = await Runner.run(agent, "Give me a JSON summary.", output_type=Summary)
```

## Multi-agent: handoff and composition

### Handoff

The triage agent routes requests to specialist agents seamlessly:

```python
from lovia.handoff import Handoff, drop_stale_tool_calls

billing = Agent(name="billing", instructions="Handle billing questions.", model="deepseek-v4-pro")
support = Agent(name="support", instructions="Handle technical issues.", model="deepseek-v4-pro")

triage = Agent(
    name="triage",
    instructions="Route to the right specialist.",
    model="deepseek-v4-pro",
    handoffs=[
        Handoff(target=billing, input_filter=drop_stale_tool_calls),
        Handoff(target=support, input_filter=drop_stale_tool_calls),
    ],
)

result = await Runner.run(triage, "I was charged twice.")
```

### Agent as tool

Wrap an agent so a parent can delegate sub-tasks to it:

```python
summarizer = Agent(name="summarizer", instructions="Summarize text.", model="deepseek-v4-pro")

orchestrator = Agent(
    name="orchestrator",
    model="deepseek-v4-pro",
    tools=[summarizer.as_tool(description="Summarize a passage of text.")],
)
```

The sub-agent runs in an isolated loop; its final output is returned as the tool result.

## Human in the loop

### Approval gates

Set `needs_approval=True` on any tool. The runner pauses until the call is
approved or denied — by your streaming consumer, a web handler, or the agent's
`approval_handler`.

### Asking the human a question

`ask_human` lets the model explicitly request input from an operator:

```python
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()
agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    tools=[ask_human(channel)],
)

# From your UI or event loop — resolve pending questions:
for q in channel.pending:
    channel.answer(q.id, "Please proceed with option A.")
```

## Hooks

`AgentHooks` fires on lifecycle events — logging, metrics, debugging:

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

Handlers may be sync or async; both work.

## Guardrails

Async callables that veto a run before it starts or after it finishes:

```python
from lovia.exceptions import GuardrailTripped


async def no_pii(messages, ctx):
    for m in messages:
        if "@" in str(m.content):
            raise GuardrailTripped("PII detected — email address in input.")


async def must_cite(output, ctx):
    if "source:" not in output.lower():
        return "Response must include a source citation."  # truthy string = violation


agent = Agent(
    name="researcher",
    model="deepseek-v4-pro",
    input_guardrails=[no_pii],
    output_guardrails=[must_cite],
)
```

Returning `None` or `False` means the check passed.

## Sessions and memory

Persist transcript state across multiple calls:

```python
from lovia.stores import SQLiteSession

session = SQLiteSession("chat.db")
await Runner.run(agent, "My project is called Atlas.", session=session, session_id="u1")
await Runner.run(agent, "What is my project called?",  session=session, session_id="u1")
```

Long-running conversations use :class:`Compaction` by default. Compaction
is **view-only and sticky**: it shapes only the transcript sent to the model
for one call and never touches the stored session, while its decisions are
remembered per run so the rendered prompt prefix stays byte-stable across
turns — provider prompt caches stay warm. Under token pressure it runs
cheap-first stages: archive huge tool results to workspace files (when a
workspace is open), replace older tool results with tiny recall markers, and
only as a last resort fold the older prefix into an incremental LLM summary.
Compaction fires in rare bursts — nothing is touched below the
``compact_at`` watermark, and a burst shrinks the prompt down to
``compact_to``.

Pass a policy explicitly when you want different thresholds or stages:

```python
from lovia import Compaction

policy = Compaction(
    context_window=200_000,  # omit to ask the provider
    compact_at=0.75,         # start compacting at 75% of the usable window
    compact_to=0.50,         # ... and shrink down to 50% (or absolute tokens)
)
result = await Runner.run(agent, "Continue.", context_policy=policy)
```

Compaction bounds what the *model* sees; the transcript itself keeps full
tool outputs (that is what makes recall and view-only safety possible). For
tools that can return huge payloads, cap what enters the transcript at the
source — ``Agent(max_tool_output_chars=...)`` or per-tool
``@tool(max_output_chars=...)`` truncate oversized outputs (head + tail, with
a marker) before they are stored, bounding memory, checkpoint, and session
cost. Built-in workspace tools are already capped via ``Workspace`` limits.

Add the opt-in ``recall_tool_result`` tool so the agent can pull back a tool
output that compaction dropped from the view, without re-running the tool:

```python
from lovia.tools import recall_tool_result

agent = Agent(name="x", tools=[..., recall_tool_result])
```

Pass ``NoopContextPolicy()`` to disable automatic context management.

## Skills

Reusable instruction bundles following the
`Agent Skills specification <https://agentskills.io/specification>`_.
Progressive disclosure keeps the context window lean: metadata is always
visible, full instructions and sub-files load on demand via tool calls.

```python
from lovia import Agent, Skills

agent = Agent(
    name="support",
    model="deepseek-v4-pro",
    skills=Skills.from_dir("./skills"),
)
```

Each skill is a directory with a ``SKILL.md`` (YAML frontmatter + body).
Optional ``references/``, ``scripts/``, and ``assets/`` subdirectories hold
supplementary resources the model loads via ``read_skill_file``.

Pass several directories to merge catalogs — ``Skills.from_dir("./skills",
"./team-skills")`` (earlier wins on name conflicts). Any frontmatter keys
beyond ``name``/``description`` (``tags``, ``version``, …) are surfaced in the
index so the model can route on them. Bodies are read lazily and never cached.

Scope which skills are exposed with a ``filter`` predicate — handy for
per-tenant or permission-based catalogs. Filtered-out skills are hidden from
the index *and* cannot be loaded::

    Skills.from_dir("./skills", filter=lambda m: "internal" not in m.extra.get("tags", []))

Custom skill sources (database, API, MCP) implement the ``SkillSource`` protocol.

## Plugins

A **plugin** bundles a feature's tools, per-turn context, system-prompt text, and
event hooks behind one object — activated fresh per run. Attach it in one line
instead of wiring each piece onto the agent separately.

The built-in **todo plugin** gives the model a `todo_write` tool and re-shows the
current checklist to it every turn, so it stays on-plan through long, multi-step
work:

```python
from lovia import Agent, Runner, todo_plugin

agent = Agent(
    name="builder",
    instructions="Complete multi-step tasks carefully.",
    model="deepseek-v4-pro",
    plugins=[todo_plugin()],
)
await Runner.run(agent, "Scaffold a REST API: model, endpoints, tests, docs.")
```

The per-turn reminder is **view-only** — injected into each model call but never
written to the transcript or session, so context never bloats as turns grow. Each
`todo_write` call still lands in the transcript (structured `list[Todo]` on the
result) for a free audit trail and automatic resume/handoff recovery. The model
decides when a checklist helps; trivial tasks get none, at zero overhead.

Watch progress live by filtering events:

```python
from lovia import events

async for ev in Runner.stream(agent, task):
    if isinstance(ev, events.ToolCallCompleted) and ev.call.name == "todo_write":
        for t in ev.result:                # list[Todo]
            print(t.status, "-", t.content)
```

Writing your own plugin is just `setup()` returning what it contributes:

```python
from lovia import InputEntry
from lovia.plugins import PluginInstance

class StayTerse:
    name = "stay_terse"

    def setup(self) -> PluginInstance:
        def remind(ctx):
            return [InputEntry(role="user", content="<reminder>Be concise.</reminder>")]
        # A PluginInstance may also carry: tools, instructions, hooks.
        return PluginInstance(view_injectors=[remind])
```

`view_injectors` run every turn and append transient entries to that one model
call only — the general seam behind ephemeral reminders.

## Built-in tools

Practical tools live under `lovia.tools` — nothing is imported automatically,
pick what you need:

```python
from lovia.tools.http import http_fetch
from lovia.tools.search import duckduckgo_search_tool
from lovia.tools.human import HumanChannel, ask_human
from lovia.tools.time import now

agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    tools=[http_fetch, duckduckgo_search_tool(), now],
)
```

Focused examples are in [`examples/tools/`](./examples/tools/).

## Workspace and coding agent

A `Workspace` gives an agent file and shell tools scoped to a directory and
a permission policy — no need to wire each tool manually:

```python
from lovia import Agent
from lovia.workspace import CommandRule, Workspace

agent = Agent(
    name="coder",
    instructions="Make small, targeted edits.",
    model="deepseek-v4-pro",
    workspace=Workspace.local(
        ".",
        mode="coding",
        denied_paths=(".env*",),
        command_rules=(
            CommandRule("git status", "allow"),
            CommandRule("rm -rf", "deny"),
        ),
    ),
)
```

| Mode | Tools exposed |
| --- | --- |
| `"readonly"` | read\_file, list\_files, grep\_files |
| `"coding"` | + write\_file, edit\_file, shell (approval by default) |
| `"trusted"` | all of the above, shell without approval by default |

The policy decides per command: `allow` runs immediately, `ask` goes through
the approval channel, `deny` is refused (compound commands like
`a && b` take the most restrictive decision across segments). `denied_paths`
globs are enforced on every file operation.

Workspace paths are root-relative. Absolute paths, `..` escapes, and symlink
escapes are rejected. The local shell still runs as the host user — the
policy is honest scoping, not OS-level isolation; hard isolation belongs to
sandboxed backends implementing the same session protocol.

## MCP

Connect to [Model Context Protocol](https://modelcontextprotocol.io) servers;
their tools appear as ordinary lovia tools. Two transports are supported —
`MCPServerStdio` (subprocess) and `MCPServerStreamableHTTP` (remote endpoint).

```bash
pip install "lovia[mcp]"
```

```python
from lovia import Agent
from lovia.mcp import MCPServerStdio

agent = Agent(
    name="assistant",
    model="openai:gpt-5.4",
    mcp_servers=[
        # The official `fetch` server pulls live data from public web APIs.
        MCPServerStdio(
            name="web",                      # prefixes tools as web__fetch
            command="uvx",
            args=["mcp-server-fetch"],
        )
    ],
)
```

By default each run opens a fresh connection and closes it afterwards (safe for
concurrent runs). To keep one connection alive across many runs, open a
**session** and put the live connection on the agent:

```python
server = MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])

async with server.session() as conn:          # opened once, reused
    agent = Agent(name="assistant", mcp_servers=[conn])
    await Runner.run(agent, "Fetch https://wttr.in/Tokyo?format=j1 and summarise it.")
    await Runner.run(agent, "...")
    tools = await conn.refresh_tools()         # re-list if the server changed
```

See `examples/26_mcp.py` for a full streaming demo.

Details:

- **Filtering** — `include_tools` / `exclude_tools` (matched on the raw MCP name).
- **Results** — text passes through; images/audio/binary become compact
  placeholders (`[image: image/png, 12.3 KB]`), never base64. Pass a
  `result_renderer` to receive the raw `MCPToolResult` and decide what the model
  sees. A tool that returns an MCP `isError` is shown to the model with a
  `[tool error] …` marker so it can self-correct.
- **Resilience** — transport failures are wrapped in `MCPError`; `auto_reconnect`
  (on by default) transparently re-establishes a dropped connection once per
  call. For `stdio`, reconnect respawns the process, so any server-side state is
  lost.

Deliberate non-goals: MCP prompts, resource browsing, sampling, OAuth, and
hosted MCP — kept out to keep the surface small.



A minimal FastAPI app with streaming, sessions, markdown rendering, and approval:

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
| `examples/01_hello.py` | minimal agent |
| `examples/02_tools.py` | custom `@tool` |
| `examples/03_streaming.py` | streaming with Rich |
| `examples/04_structured_output.py` | Pydantic output |
| `examples/05_handoff.py` | agent handoff |
| `examples/08_skills.py` | Skills capability |
| `examples/11_approval.py` | tool approval |
| `examples/27_todos.py` | todo plugin / checklist |
| `examples/16_web_serve.py` | web UI |
| `examples/22_workspace.py` | direct workspace session |
| `examples/23_workspace_agent.py` | coding agent |
| `examples/26_mcp.py` | remote MCP server (fetch) + streaming |
| `examples/24_prefect.py` | Prefect workflow |
| `examples/tools/` | focused tool demos |
| `examples/workflows/` | workflow patterns |

## Development

```bash
pip install -e ".[dev]"

ruff check .          # lint
ruff format .         # format
mypy lovia            # type-check
pytest -q             # run tests
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
