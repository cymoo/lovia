# lovia

[![PyPI](https://img.shields.io/pypi/v/lovia)](https://pypi.org/project/lovia/)
[![Python](https://img.shields.io/pypi/pyversions/lovia)](https://pypi.org/project/lovia/)
[![CI](https://github.com/cymoo/lovia/actions/workflows/ci.yml/badge.svg)](https://github.com/cymoo/lovia/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/cymoo/lovia/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-cymoo.github.io%2Flovia-black)](https://cymoo.github.io/lovia/)

**English** | [简体中文](https://github.com/cymoo/lovia/blob/main/README-zh.md)

**One agent loop, three ways to use it.** lovia is a Python agent framework you
can import as a library, run as a ready-made assistant in your browser, or take
apart to build your own. Each layer is built on the one below, so going from a
script to a product never means switching frameworks. The core depends only on
`httpx`, `pydantic` and `pyyaml`; everything else is opt-in.

| Use lovia as… | What you get | Start with |
| --- | --- | --- |
| [**A library**](https://github.com/cymoo/lovia#a-library) | An agent loop in ordinary Python: tools, streaming, sessions, approvals, context management | `pip install lovia` |
| [**A ready-made harness**](https://github.com/cymoo/lovia#a-ready-made-harness-lovia-web) | A local assistant that works in your project folders, with files, shell, memory, skills and MCP | `pip install "lovia[web]"`, then `lovia web` |
| [**The base for your own**](https://github.com/cymoo/lovia#the-base-for-your-own-harness) | That assistant as a toolkit: add tools, brand the page, add views, or keep only the API | `lovia[web]`, then `default_app()` or `create_app()` |

![The lovia web UI](https://raw.githubusercontent.com/cymoo/lovia/main/docs/images/web-ui.webp)

*`lovia web`: chats grouped by workspace, a chart a tool drew, the agent's plan
above the composer, and the diff of the report it just updated.*

## A library

```bash
pip install lovia
export OPENAI_BASE_URL=https://your-endpoint/v1  # any OpenAI-compatible endpoint
export OPENAI_API_KEY=...
```

```python
from lovia import Agent, tool


@tool
def get_order(order_id: str) -> str:
    """Look up an order's status by id."""
    return f"Order {order_id}: shipped 2 days ago, arriving Thursday."


agent = Agent(
    name="support",
    instructions="Look the order up before answering, in one or two sentences.",
    model="glm-5.2",  # a model your endpoint serves
    tools=[get_order],
)

print(agent.run_sync("Where is my order A-1042?").output)
```

From async code, use `await Runner.run(agent, ...)`, or `Runner.stream(...)` for
typed events. For Anthropic, set `ANTHROPIC_API_KEY` and write
`model="anthropic:<model>"`
([configure a model](https://cymoo.github.io/lovia/quickstart/#2-configure-a-model)).

What the loop gives you, each a few lines away:

- [**Tools**](https://cymoo.github.io/lovia/tools/) from typed functions, run
  concurrently, and [**structured output**](https://cymoo.github.io/lovia/structured-output/)
  validated into Pydantic models.
- [**Multi-agent**](https://cymoo.github.io/lovia/multi-agent/) by handoff,
  agent-as-tool, or background subagents that report back.
- [**Approvals**](https://cymoo.github.io/lovia/tools/#tool-approval) for
  sensitive tools: answer from any UI, or suspend the run until someone does.
- [**Sessions and checkpoints**](https://cymoo.github.io/lovia/sessions-and-checkpoints/):
  conversations persist, and a crashed run resumes where it stopped.
- [**Context compaction**](https://cymoo.github.io/lovia/context/) that never
  rewrites the transcript and keeps provider prompt caches warm.
- [**A workspace**](https://cymoo.github.io/lovia/workspace/): file and shell
  tools under one policy for paths and commands, locally, over SSH, or in a
  container.
- [**Plugins**](https://cymoo.github.io/lovia/plugins/): Skills, MCP, Todo and
  long-term Memory, built on the same extension point you get.
- [**Guardrails**](https://cymoo.github.io/lovia/guardrails/),
  [**retries and budgets**](https://cymoo.github.io/lovia/budgets/),
  [**cancellation and steering**](https://cymoo.github.io/lovia/cancellation/),
  and [**offline testing**](https://cymoo.github.io/lovia/testing/) with
  [**evals**](https://cymoo.github.io/lovia/eval/).

**→ [Cheatsheet](https://cymoo.github.io/lovia/cheatsheet/)**, every feature in
one snippet · [Quickstart](https://cymoo.github.io/lovia/quickstart/) ·
[Examples](https://github.com/cymoo/lovia/tree/main/examples), a numbered path
of runnable scripts

## A ready-made harness: `lovia web`

```bash
pip install "lovia[web]"
lovia web
```

Open `http://127.0.0.1:8000` and add a model on first launch. New chats work in
the directory you started from; `lovia web ~/code/app` picks another.

- **It works in your folders.** It reads and edits files, runs commands, plans
  with a todo list, searches the web (through a hosted search service —
  Parallel by default, no key needed; Settings → Web search picks another or
  turns it off), draws charts and tables, and hands independent work to
  background subagents. A panel follows what runs in the
  background: each command's output, each task's progress and report.
- **Many projects at once.** The sidebar groups chats by workspace. Each chat
  has its own folder, model and permission mode, and the folder can be on an
  SSH host or in a container. Ask the assistant about your workspaces or an
  earlier chat and it looks them up.
- **You stay in control.** Permission modes range from read-only to full
  access. In the default mode, irreversible commands ask first and say why,
  and an approval can be remembered for the chat; in every mode, dangerous
  commands are refused.
- **It follows your project.** A chat loads its folder's `AGENTS.md`,
  `.agents/skills` and `.mcp.json`. In the composer, `/` lists commands and
  skills and `@` mentions a file.
- **A complete chat.** Streaming, branching from any reply, steering a run
  while it works, attachments, scheduled runs, and long-term memory you can
  edit. A run keeps going when you close the tab. Optional voice input turns
  speech into text through a transcription service you choose.
- **Settings without restarts.** Models, MCP servers, skills and the
  assistant's instructions live in Settings. Everything is kept in one home
  directory, `~/.lovia`, never in your repository.

On `127.0.0.1` no credentials are needed; binding any other address requires a
token. `lovia web --help` lists the flags.

**→ [Web UI guide](https://cymoo.github.io/lovia/web-ui/)**

## The base for your own harness

`lovia web` is assembled from public pieces. Pick the depth you need:

| You want | Use | What you keep |
| --- | --- | --- |
| The same assistant, plus your tools | `default_app(customize=...)` | All of `lovia web`: Settings, memory, workspaces, schedules |
| Your own agent in a branded, extended chat page | `create_app(agent, ui=ChatUI(...))` | The chat page: streaming, approvals, history, files |
| Your own front end | `create_app(agent, ui=False)` or `build_api_router()` | The JSON + SSE API behind the page (`/api/docs`) |

**The same assistant, plus your tools.** `default_app()` builds exactly what
`lovia web` serves, and `customize` hands you each agent it builds:

```python
from lovia import tool
from lovia.web import default_app, serve


@tool
def order_status(order_id: str) -> str:
    """Look up an order."""
    return "shipped"


serve(default_app(customize=lambda agent: agent.clone(tools=[*agent.tools, order_status])))
```

**Your agent, your page.** `create_app(agent)` serves the agent you built,
exactly as you built it. It returns a FastAPI app, so your own routes sit
beside it:

```python
from lovia.web import ChatUI, create_app, serve

app = create_app(
    agent,
    title="Acme",
    auth=my_auth,          # any FastAPI dependency
    run_deps=my_run_deps,  # who is asking, and what the page picked -> ctx.deps
    ui=ChatUI(assets="ui", styles=["theme.css"], logo="logo.svg", scripts=["acme.js"]),
)
serve(app)
```

- **Tool results that draw UI.** A tool returns `ui_result(kind, props)`.
  Tables, ECharts charts and sandboxed HTML pages are built in.
- **A scriptable page.** Through `window.lovia`, your scripts add renderers,
  tool views, slash commands, and whole views beside the chat. Template blocks,
  design tokens and `data-i18n` keys change the page's markup, look and wording.
  `lovia web init-ui ui` scaffolds the directory.
- **Worked examples.** Examples `26`–`27` and `31`–`35` cover serving,
  components, branding and extensions.
  [lovia-react](https://github.com/cymoo/lovia-react) extends the bundled chat
  with React views.

**Your own front end.** `create_app(agent, ui=False)` serves only the API.
`build_api_router()` mounts it in your own FastAPI app, behind your own auth.

**→ [Web server guide](https://cymoo.github.io/lovia/web-server/)** ·
[HTTP API](https://cymoo.github.io/lovia/http-api/) ·
[Deployment](https://cymoo.github.io/lovia/deployment/)

## Design principles

- **Concise.** An immutable `Agent`, a stateless `Runner`, `@tool`, and plugins
  as the one extension axis. Every piece fits one screen of mental model.
- **Lightweight.** Three hard dependencies. MCP, the web app and search
  backends are extras, imported only when used.
- **Readable.** Model calls, tool execution, retries and persistence run in one
  explicit loop. When something surprises you, there is one chain to follow.
- **Extensible, with restraint.** Extension points are dataclasses, Protocols
  and decorators, never subclassing. What fits in a short recipe on your side
  stays out of the framework.

## Install

lovia needs Python 3.10 or newer.

| Need | Install |
| --- | --- |
| Core library | `pip install lovia` |
| MCP | `pip install "lovia[mcp]"` |
| Web app (includes MCP) | `pip install "lovia[web]"` |
| DuckDuckGo search | `pip install "lovia[ddg]"` (Tavily needs no extra: set `TAVILY_API_KEY`) |
| Runnable examples | `pip install "lovia[examples,web]"` |

## Status

lovia is in beta, on its way to 1.0. Until then, breaking changes can ship in
patch releases; pin a version if that matters to you.
[Stability](https://cymoo.github.io/lovia/stability/) lists what 1.0 will
promise to keep.

## Contributing

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format .
.venv/bin/python -m mypy lovia
```

Live provider tests are skipped unless enabled:
`LOVIA_LIVE_TESTS=1 .venv/bin/python -m pytest -m live_provider`. Read
[Architecture](https://cymoo.github.io/lovia/architecture/) before changing
the internals.

## License

[MIT](https://github.com/cymoo/lovia/blob/main/LICENSE)
