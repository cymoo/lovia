# lovia examples

Every file is a small, self-contained, runnable script for one feature.
Read them in order and you have a tour of the framework; copy any one of
them and you have a starting point.

## Setup

```bash
pip install -e ".[examples,web]"     # from the repo root
cp .env.example .env                 # then set LOVIA_MODEL + your API key
python examples/01_hello.py
```

`LOVIA_MODEL` picks the model for every example, e.g. `openai:gpt-5.5` or
`anthropic:claude-4-8-opus`. Point `OPENAI_BASE_URL` at any OpenAI-compatible
service (DeepSeek, Ollama, vLLM, …) to use it with `openai:<model>` strings.
Scripts that write anything write into `tmp/` (gitignored).

Two examples run fully offline, no key needed: `10_custom_provider.py` and
`28_eval.py`. `19_workspace.py` needs no model either.

## Learning path

### Fundamentals

| File | What it shows |
| --- | --- |
| `01_hello.py` | minimal agent, one model call |
| `02_tools.py` | `@tool` functions: typed schemas, sync/async, error semantics |
| `03_streaming.py` | consume the typed event stream |
| `04_structured_output.py` | validated output, per-call `output_type` override |
| `05_sessions.py` | persisted multi-turn chat with `SQLiteSession` |
| `06_multimodal.py` | image input via `ImagePart` |

### Multi-agent

| File | What it shows |
| --- | --- |
| `07_handoff.py` | transfer control to a specialist (`Handoff` customization) |
| `08_agent_as_tool.py` | delegate a bounded subtask to a sub-agent |

### Models & providers

| File | What it shows |
| --- | --- |
| `09_model_settings.py` | `ModelSettings`, `provider_options`, OpenAI-compatible endpoints |
| `10_custom_provider.py` | implement the `Provider` protocol (offline) |

### Control & production

| File | What it shows |
| --- | --- |
| `11_hooks.py` | observe every run event with `AgentHooks` |
| `12_approval.py` | human-in-the-loop approval, predicate gating |
| `13_guardrails.py` | input/output guardrails |
| `14_reliability.py` | budgets, provider+tool retries, timeouts, cancel, fallback |
| `15_resume.py` | checkpoint a run, kill it mid-flight, resume it |
| `16_steering.py` | inject user messages into a live run (`Mailbox`) |
| `17_context_compaction.py` | long chats that survive the context window |
| `18_dependencies.py` | per-run deps in instructions and tools (`RunContext`) |

### Workspace & plugins

| File | What it shows |
| --- | --- |
| `19_workspace.py` | the workspace as a plain library (no agent) |
| `20_workspace_agent.py` | coding agent with file/shell tools + command policy |
| `21_todos.py` | `Todo` plugin: externalized plan, per-turn reminders |
| `22_skills.py` | reusable skill bundles with progressive disclosure |
| `23_memory.py` | long-term memory across runs (`Memory` plugin) |
| `24_mcp.py` | tools from an MCP server |
| `25_custom_plugin.py` | write your own plugin (tools + injector + teardown) |

### Serving & apps

| File | What it shows |
| --- | --- |
| `26_web_serve.py` | built-in chat UI over HTTP (`pip install "lovia[web]"`) |
| `27_web_api.py` | JSON + SSE API only; bring your own front-end |
| `28_eval.py` | offline evals: checks, LLM judge, baseline diff |
| `29_data_analysis.py` | data-analysis agent over SQLite + chart report |
| `30_support_bot.py` | capstone: interactive terminal support bot |

## Subdirectories

- [`tools/`](tools/) — one script per built-in tool family (HTTP, time,
  search, ask-a-human).
- [`workflows/`](workflows/) — the patterns from Anthropic's *Building
  effective agents* (chaining, routing, parallelization, orchestrator,
  evaluator loop, autonomous agent) in plain Python.
- [`skills/`](skills/) — the sample skill directory used by `22_skills.py`.
