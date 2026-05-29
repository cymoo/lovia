# AGENTS.md

Instructions for AI coding assistants working on this repository.

## Project overview

**lovia** is a lightweight, provider-neutral agent framework for Python. Core is under ~2000 lines; hard dependencies are only `httpx` and `pydantic`.

## Setup

```bash
pip install -e .[dev]
# Optional MCP support
pip install -e .[mcp]
# Optional web layer (FastAPI + chat UI)
pip install -e .[web]
```

Requires Python 3.10+.

## Commands

| Task | Command |
| --- | --- |
| Run tests | `pytest` |
| Lint | `ruff check .` |
| Format | `ruff format .` |
| Type-check | `mypy lovia` |

## Architecture

```
lovia/
  agent.py          # Agent dataclass — main user-facing config
  runner.py         # Runner — orchestrates the agent loop
  tools/            # @tool decorator, Tool type, and opt-in tool factories
    __init__.py     #   core Tool/tool API + public re-exports
    read_file.py    #   sandbox-backed file tools (read, write, edit, list, glob, shell)
    write_file.py
    edit_file.py
    list_dir.py
    glob.py
    shell.py
    coding_tools.py #   coding_tools() convenience factory
    http.py         #   http_fetch
    search.py       #   duckduckgo_search_tool  (requires lovia[tools])
    todo.py         #   TodoList + todo_tools
    human.py        #   HumanChannel + ask_human
    think.py        #   think
    time.py         #   now
  messages.py       # ChatMessage, ToolCall, Usage types
  events.py         # Streaming event types
  output.py         # Structured output handling
  handoff.py        # Handoff + agent_as_tool
  hooks.py          # AgentHooks subscriber
  guardrails.py     # input/output guardrail protocol
  session.py        # Session protocol
  context_policy.py # ContextPolicy — keeps long conversations under the model's window
  skills.py         # Skill / SkillCatalog (SKILL.md, lazy/eager modes)
  schema.py         # JSON Schema generation from Python types
  exceptions.py     # Framework exceptions (carry an optional .hint)
  mcp.py            # Optional MCP client (requires mcp package)
  providers/        # LLM provider adapters (OpenAI, Anthropic, …)
  stores/           # Session and memory store implementations
  sandbox/          # Filesystem + process sandbox (Sandbox.local,
                    #   SandboxBackend/SandboxSession protocols)
  web/              # Optional FastAPI + SSE layer + Jinja2 chat UI
                    #   (decoupled from core; only loaded when lovia[web] is used)
```

Three layers, each strictly downstream of the previous: **core** (everything
outside `sandbox/` and `web/`), **sandbox** (fs + exec), **web** (HTTP/SSE/UI).
Core never imports sandbox or web.

## Conventions

- **Async-only** public API. All runner methods are `async`; use `asyncio.run()` in scripts.
- **Pydantic v2** for data models. Prefer `model_fields`, `model_validate`, etc.
- **Type annotations** on all public functions and classes.
- Keep the core minimal — avoid adding new hard dependencies without a strong reason.
- Provider adapters live under `lovia/providers/`; each adapter translates between the lovia message format and the vendor API.
- Tests live under `tests/`; use `pytest-asyncio` (already configured with `asyncio_mode = auto`).
- Follow `ruff` rules for formatting and linting. Do not add `# noqa` suppressions without a comment explaining why.

## Design philosophy

lovia is built around four words. When in doubt, optimise for the one earlier in the list.

1. **Concise (简洁).** Every piece should fit on one screen of mental model. The core (`agent.py`, `runner.py`, `tools/`, `output.py`, `schema.py`, `skills.py`, `exceptions.py`) stays small and obvious. New features must justify their line cost; cleverness that saves keystrokes but obscures behaviour is rejected.
2. **Lightweight (轻量).** Core has exactly two hard dependencies: `httpx` and `pydantic`. Every other capability — MCP, web UI, DuckDuckGo, etc. — is an opt-in extra and only imported when the user asks for it. `import lovia` must stay cheap.
3. **Extensible (易扩展).** Public surfaces are dataclasses, Protocols, and `@decorator` hooks — not subclasses you must inherit from. Providers, sessions, memory stores, web-search backends, and hooks are all Protocol-based; users plug in their own implementations without monkey-patching.
4. **General-purpose (通用).** `lovia.tools.*` ships practical, framework-agnostic tools (http, search, todo, human-in-the-loop, think, time, filesystem, shell), and `lovia.sandbox.*` ships the filesystem + process boundary so a real agent can be assembled in minutes. Optional integrations such as web, Rich examples, and Prefect examples stay behind extras.

A few corollaries that follow from these:

- **Decline before designing.** If a feature looks indispensable, check whether it can be a 10-line user-side recipe instead of a framework abstraction.
- **Async-only public API.** Sync helpers (`Runner.run_sync`, `Agent.run_sync`) wrap `asyncio.run`; we never duplicate logic across sync and async code paths.
- **Errors are debugging surfaces.** Every `LoviaError` carries an optional `.hint`; `OutputValidationError` also exposes the raw model text and target schema name. Bury no context.
- **Backwards compatibility is a feature.** Renames go through a deprecation shim for at least one minor release.

## Git commit convention

Follow [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): imperative summary`.
Common types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`.
