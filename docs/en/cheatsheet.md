# Cheatsheet

The whole of lovia on one page: one short snippet per feature, each linked to
its full guide. Replace `<model>` with a model your endpoint serves
([configure a model](quickstart.md#2-configure-a-model)). Snippets that `await`
run inside async code; `agent` is any Agent from above.

| Import from | What lives there |
| --- | --- |
| `lovia` | `Agent`, `Runner`, `tool`, `events`, sessions, checkpoints, policies, built-in plugins |
| `lovia.tools` | Built-in tools: `web_fetch`, `web_search`, `ask_human`, `fence`, … |
| `lovia.workspace` | `Workspace`, `RemoteHost`, `CommandRule`; rule bundles in `lovia.workspace.rules` |
| `lovia.plugins.mcp` | `MCP`, `MCPServerStdio`, `MCPServerStreamableHTTP` (`lovia[mcp]`) |
| `lovia.web` | `create_app`, `default_app`, `serve`, `ChatUI`, `ui_result` (`lovia[web]`) |
| `lovia.testing` · `lovia.eval` | `ScriptedProvider` · `evaluate`, `Case`, checks |

## The library

### Agents

An `Agent` is configuration, not state: share it freely and vary it with
`clone()`. Instructions can be computed per run from `ctx.deps`.

```python
from dataclasses import dataclass
from lovia import Agent, RunContext, Runner, tool


@dataclass
class Deps:
    user: str


@tool
def get_order(ctx: RunContext[Deps], order_id: str) -> str:
    """Look up one of the signed-in user's orders."""
    return f"{ctx.deps.user}'s order {order_id}: shipped"


agent = Agent[Deps](
    name="support",
    instructions="Look the order up before answering.",
    model="<model>",
    tools=[get_order],
)


@agent.instruction
def address(ctx: RunContext[Deps]) -> str:
    return f"Address the user as {ctx.deps.user}."


terse = agent.clone(name="support-terse", instructions="Answer in one line.")
result = await Runner.run(agent, "Where is A-1042?", deps=Deps(user="Alex"))
```

→ [Agents](agents.md)

### Running and streaming

`agent.run_sync()` for scripts, `Runner.run()` from async code,
`Runner.stream()` for events. A stream never raises while you iterate: it ends
with exactly one terminal event, and `await handle.result()` gives the result.

```python
from lovia import ImagePart, Runner, TextPart, events, user

print(agent.run_sync("Why is the sky blue?").output)

handle = Runner.stream(agent, "How long does light take to reach Mars?")
async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)
result = await handle.result()

photo = user([TextPart(text="What's in this picture?"), ImagePart(url="https://…/cat.jpg")])
result = await Runner.run(agent, [photo])
```

→ [Running](running.md) · [Streaming](streaming.md)

### Tools

`@tool` builds the schema from the signature, docstring, `Annotated` and
Pydantic `Field`. One turn's calls run concurrently; `parallel=False` makes a
tool a barrier. `fence()` marks fetched text as material, not instructions.

```python
from typing import Annotated
from pydantic import Field
from lovia import tool
from lovia.tools import fence


@tool(strict=True)
def search_docs(
    query: Annotated[str, "Search terms"],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> str:
    """Search internal documentation."""
    return fence(lookup(query, limit))


@tool(parallel=False)
async def apply_migration(name: str) -> str:
    """Apply a database migration (never concurrently with other tools)."""
    return "applied"
```

→ [Tools](tools.md) · [Built-in tools](built-in-tools.md)

### Structured output

Pass a Pydantic model, dataclass, `TypedDict` or plain type; the final answer
is validated, and repaired once by default when it doesn't parse.

```python
from pydantic import BaseModel
from lovia import Agent, Runner


class Brief(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(name="summarizer", model="<model>", output_type=Brief)
result = await Runner.run(agent, "Summarize the Transformer paper.")
result.output.title
```

→ [Structured output](structured-output.md)

### Providers

A bare model name goes to the OpenAI-compatible endpoint in `OPENAI_BASE_URL` /
`OPENAI_API_KEY`; `anthropic:` goes to Anthropic (`ANTHROPIC_API_KEY`, plus
`ANTHROPIC_BASE_URL` for another endpoint). A provider instance or your own
`Provider` protocol works too.

```python
from lovia import Agent, ModelSettings, model_from_env

agent = Agent(
    name="assistant",
    model="anthropic:<model>",
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
agent = Agent(name="assistant", model=model_from_env())  # reads LOVIA_MODEL
```

→ [Providers & models](providers.md)

### Multi-agent

| Your task | Use | What happens |
| --- | --- | --- |
| A specialist takes over the conversation | `handoffs=[...]` | It gets the full history and continues the same run |
| The parent needs a subtask's result first | `agent.as_tool()` | The child sees only the delegated prompt; its answer is a tool result |
| Independent work in the background | `Subagents([...])` | The child runs on its own and reports back later |

```python
from lovia import Agent, Subagents

researcher = Agent(name="researcher", instructions="Research and report.", model="<model>")

router = Agent(name="router", model="<model>", handoffs=[researcher])
manager = Agent(name="manager", model="<model>",
                tools=[researcher.as_tool(description="Research a topic.")])
assistant = Agent(name="assistant", model="<model>", plugins=[Subagents([researcher])])
```

→ [Multi-agent](multi-agent.md)

### Approvals

`permission="ask"` gates a tool. Answer from your UI, a policy or another
channel; an unanswered request is denied, so a run never hangs, and a denial's
reason reaches the model. `ev.suspend()` parks the run; resuming its
checkpoint asks again.

```python
from lovia import Runner, events, tool


@tool(permission="ask")
async def refund(order_id: str, amount_cents: int) -> str:
    """Issue a refund."""
    return "refunded"


async for ev in Runner.stream(agent.clone(tools=[refund]), "Refund order A123."):
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()  # or ev.reject("offer store credit"), ev.suspend()
```

→ [Tool approval](tools.md#tool-approval)

### Sessions and checkpoints

A session carries a conversation across runs. A checkpoint makes one run
crash-safe and idempotent: re-running a finished `run_id` replays its result
without calling the model.

```python
from lovia import CheckpointOptions, Runner, SQLiteCheckpointer, SQLiteSession

session = SQLiteSession("chat.db")
await Runner.run(agent, "My project is called Atlas.", session=session, session_id="u1")
await Runner.run(agent, "What's my project called?", session=session, session_id="u1")

cp = SQLiteCheckpointer("runs.db")
await Runner.run(agent, "Migrate the reports.", checkpoint=CheckpointOptions(cp, "job-42"))
```

→ [Sessions & checkpoints](sessions-and-checkpoints.md)

### Context compaction

Compaction shapes only what the next model call sees — never the transcript —
and keeps the prompt prefix stable for provider caches.

```python
from lovia import Agent, Compaction

agent = Agent(
    name="companion",
    model="<model>",
    context_policy=Compaction(context_window=200_000, compact_at=0.85, compact_to=0.60),
)
```

→ [Context management](context.md)

### Guardrails, retries, budgets

Guardrails veto at the input/output boundary. Retry posture belongs to the
agent; limits belong to the run.

```python
from lovia import Agent, RetryPolicy, RunBudget, Runner


async def must_cite(output, ctx):
    if "source:" not in str(output).lower():
        return "Missing source citation."  # a string trips the guardrail


agent = Agent(name="researcher", model="<model>",
              output_guardrails=[must_cite], retry=RetryPolicy(max_attempts=2))
await Runner.run(agent, "Analyze these logs.",
                 budget=RunBudget(max_tool_calls=20, max_seconds=60))
```

→ [Guardrails](guardrails.md) · [Provider retries](retries.md) · [Budgets & limits](budgets.md)

### Hooks, cancellation, steering

Hooks observe every run event (the same types as the stream). A cancel token
stops a run — and its sub-runs; a mailbox pushes a message into a live run,
which the model sees next turn.

```python
from lovia import AgentHooks, CancelToken, Mailbox, RunContext, Runner, events

hooks = AgentHooks()


@hooks.on(events.TurnStarted)
def last_call(ev, ctx: RunContext):
    if ev.turn == 10:  # turns count from 1; max_turns below
        ctx.mailbox.push("Last turn: answer with what you have.")


token, mailbox = CancelToken(), Mailbox()
handle = Runner.stream(agent.clone(hooks=hooks), "Analyze these logs.",
                       max_turns=10, cancel_token=token, mailbox=mailbox)
mailbox.push("Focus on the 5xx spike around 14:00.")  # seen next turn
async for ev in handle:
    ...  # meanwhile, from a stop button: token.cancel("user clicked stop")
```

→ [Observability](observability.md) · [Cancellation & steering](cancellation.md)

### Workspace

File and shell tools scoped to a root, governed by one `allow` / `ask` /
`deny` policy over paths and commands; `ask` uses the approval channel above.

```python
from lovia import Agent
from lovia.workspace import CommandRule, RemoteHost, Workspace
from lovia.workspace.rules import DEV_TOOLS, READ_ONLY

agent = Agent(
    name="coder",
    model="<model>",
    workspace=Workspace.local(
        ".",
        mode="coding",  # or "readonly", "trusted"
        command_rules=READ_ONLY + DEV_TOOLS + (CommandRule("gh", "ask"),),
    ),
)
remote = Workspace.remote(RemoteHost.ssh("box"), "/srv/project")  # same tools, run there
```

- **Modes.** `readonly` reads inside the root, with no shell; `coding` (the
  default) edits inside the root and reads anywhere; `trusted` writes anywhere
  and runs commands unasked.
- **Commands.** Under `coding` a command asks unless a rule allows it —
  `READ_ONLY` and `DEV_TOOLS` are the stock bundles. Irreversible commands
  (`git push`, `rm -r`, `sudo`) ask with a reason.
- **Baseline.** Under every mode, dangerous commands (`git push --force`,
  `rm -rf /`) are denied, credential stores are off-limits, and writes to `.git`
  or CI config ask.
- **Processes.** `shell(background=true)` runs dev servers and watchers as
  session-owned processes; read them with `read_process_output`, stop them with
  `kill_process`.
- **Environment.** Commands get a minimal environment — no API keys or proxy
  settings; pass what they need with `env={...}`.
- **Edits.** `write_file` and `edit_file` refuse a file that changed on disk
  since it was last read; `grep_files` skips what git ignores.
- **Remote.** A container or SSH host needs only `python3` ≥ 3.10; the policy
  is enforced over there.

→ [Workspace](workspace.md)

### Plugins

A plugin contributes tools, prompt text, per-turn view injectors, hooks and
guardrails — never control flow. Todo, Skills, MCP and Memory are all plugins.

```python
from dataclasses import dataclass
from datetime import datetime
from lovia import Agent, PluginInstance, RunContext, Skills, Todo, tool
from lovia.plugins.mcp import MCP, MCPServerStdio


@tool
def now() -> str:
    """The current local time."""
    return datetime.now().isoformat(timespec="minutes")


@dataclass
class Clock:  # your own: a name plus an async setup()
    name: str = "clock"

    async def setup(self, ctx: RunContext) -> PluginInstance:
        return PluginInstance(tools=[now], instructions="Call `now` for the time.")


agent = Agent(
    name="builder",
    model="<model>",
    plugins=[
        Todo(),                 # a checklist for multi-step work
        Skills("./skills"),     # SKILL.md bundles, loaded on demand; "/name" asks for one
        MCP(MCPServerStdio(name="fetch", command="uvx", args=["mcp-server-fetch"])),
        Clock(),
    ],
)
```

→ [Plugins](plugins.md) · [Todo](todo.md) · [Skills](skills.md) · [MCP](mcp.md)

### Memory

Always-in-prompt **notes** (facts the user stated or confirmed, pinned per
session so they never bust the cache) plus a searchable **archive**. A
periodic **dream** pass merges duplicates and prunes stale notes.

```python
from lovia import Agent, Memory, OpenAIEmbedder

agent = Agent(name="assistant", model="<model>", plugins=[Memory("./memory")])

Memory("./memory")                                      # keyword recall (SQLite FTS5)
Memory("./memory", embedder=OpenAIEmbedder())           # + semantic: hybrid recall
Memory("./memory", index=None)                          # notes only, no archive
Memory("./memory", scope=lambda ctx: ctx.deps.project)  # + per-project notes and recall
```

→ [Memory](memory.md)

### Testing and evals

`ScriptedProvider` runs the real loop and real tools against canned model
replies — offline and deterministic. `evaluate()` scores behavior; any
function is a check, and the LLM judge is just another one.

```python
from lovia import Agent, Runner, tool
from lovia.eval import Case, contains, evaluate, llm_judge, tool_called
from lovia.testing import ScriptedProvider, call, text


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


agent = Agent(name="calc", model="<model>", tools=[add])

# Offline: canned replies drive the real loop and the real `add`.
offline = agent.clone(model=ScriptedProvider([call("add", {"a": 2, "b": 3}), text("5")]))
assert (await Runner.run(offline, "What is 2 + 3?")).output == "5"

# Against the real model: behavior, sampled and scored.
report = await evaluate(agent, [
    Case("What is the capital of France?", checks=[contains("Paris")]),
    Case("What's 1234 + 5678?", checks=[tool_called("add")]),
    Case("Write a haiku about spring", checks=[llm_judge("A 5-7-5 haiku about spring")],
         samples=4, pass_threshold=0.75),
])
assert report.passed
```

→ [Testing](testing.md) · [Evals](eval.md)

## `lovia web`

The ready-made assistant: chat, workspace tools, memory, skills, MCP,
schedules and subagents (`pip install "lovia[web]"`).

| Command | Does |
| --- | --- |
| `lovia web` | Serve on `http://127.0.0.1:8000`; new chats work in the current directory |
| `lovia web ~/code/app` | New chats work in that directory |
| `lovia web --home ./.lovia` | Keep config, history and memory there instead of `~/.lovia` |
| `lovia web --readonly` / `--lock-workspace` / `--no-workspace` | Narrow what chats may do and where |
| `lovia web --host 0.0.0.0 --token …` | Serve beyond loopback (a token is required, generated if omitted) |
| `lovia web --check` | Probe the configured model endpoint and exit |
| `lovia web --app app:agent` | Serve your own Agent, `{name: agent}` mapping, or app |
| `lovia web init-ui ui` | Scaffold a page customization directory |

- **Settings, not flags.** Models, MCP servers, skills, web search and the
  assistant's instructions, permission mode and switches live in Settings and
  apply from the next run.
- **Per project.** The sidebar groups chats by workspace, so several projects
  live side by side. A chat follows its folder's `AGENTS.md` (from the git root
  down), `.agents/skills` and `.mcp.json`; the folder can be on an SSH host or
  in a container.
- **In the composer.** `@` mentions a file, `/` lists commands and skills, and
  the chips pick the folder, permission mode and model per chat.

→ [Web UI](web-ui.md)

## Building on lovia web

Three depths, from "the same app plus my tools" to "only the API".

### The same assistant, plus your tools

`default_app()` is what `lovia web` serves; `customize` hands you each agent it
builds.

```python
from lovia import tool
from lovia.web import default_app, serve


@tool
def order_status(order_id: str) -> str:
    """Look up an order."""
    return "shipped"


def customize(agent):
    return agent.clone(tools=[*agent.tools, order_status])


serve(default_app(workspace=".", customize=customize))
```

→ [Two ways in](web-server.md#two-ways-in)

### Your agent, your page

`create_app(agent)` serves the agent you built, exactly as built. It returns a
FastAPI app, so your own routes sit beside it.

```python
from lovia.web import ChatUI, create_app, serve

app = create_app(
    agent,
    title="Acme",
    auth=my_auth,          # any FastAPI dependency
    run_deps=my_run_deps,  # RunTrigger -> ctx.deps: who is asking, what the page picked
    ui=ChatUI(assets="ui", styles=["theme.css"], logo="logo.svg", scripts=["acme.js"]),
)


@app.get("/acme/stats")
def stats() -> dict: ...


serve(app)
```

A tool result can draw UI: return `ui_result(kind, props)`. Tables, ECharts
charts and sandboxed HTML pages are built in.

```python
from lovia import tool
from lovia.web import ui_result


@tool
def top_products(n: int = 10) -> str:
    """Best sellers, shown to the user as a table."""
    return ui_result("table", {"columns": ["sku", "units"], "rows": query(n)})
```

The page's scripts use `window.lovia` — renderers for your own kinds, tool
views, slash commands, and whole views beside the chat:

```js
lovia.registerRenderer('kpis', { render(el, props) { /* draw props */ } });
lovia.registerToolView('order_status', { summary: (t) => `Order ${t.args?.order_id}` });
lovia.registerCommand('ticket', {
  description: 'Open a ticket', argument: 'Subject', run: ({ args }) => openTicket(args),
});
lovia.registerView('dashboard', { title: 'Dashboard', render: (el) => drawDashboard(el) });
```

[lovia-react](https://github.com/cymoo/lovia-react) is a worked example: the
bundled chat, extended with React pages drawn as lovia views.

→ [Customizing the page](web-server.md#customizing-the-page) ·
[UI components](web-server.md#ui-components) ·
[Run deps](web-server.md#run-deps) ·
[Authentication](web-server.md#authentication)

### Only the API

Everything the page does is a JSON + SSE API (`/api/docs`). Turn the page off,
or mount the router into your own FastAPI app — where the router alone has no
auth, so put yours in front:

```python
from fastapi import FastAPI
from lovia.web import ChatStore, RouterDeps, build_api_router, create_app

# Either: the API alone, with create_app's token / auth options.
app = create_app(agent, ui=False)

# Or: the router inside your own app, your middleware and auth.
deps = RouterDeps(agents={"bot": agent}, store=ChatStore.sqlite("chats.db"))
app = FastAPI(lifespan=deps.lifespan)
app.include_router(build_api_router(deps))
```

```bash
curl -N http://127.0.0.1:8000/api/chat/stream \
  -H 'content-type: application/json' -d '{"message": "Hello"}'
```

→ [HTTP API](http-api.md) · [Deployment](deployment.md)
