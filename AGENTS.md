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
  agent.py       # Agent dataclass (the main user-facing config)
  runner.py      # Runner — orchestrates the agent loop
  tools/         # @tool decorator, Tool type, and opt-in tool factories
  messages.py    # ChatMessage, ToolCall, Usage types
  events.py      # Streaming event types
  output.py      # Structured output handling
  handoff.py     # Handoff between agents
  hooks.py       # AgentHooks base class
  session.py     # Session protocol
  context_policy.py # ContextPolicy — keeps long conversations under the model's window
  skills.py      # Skill / SkillCatalog (SKILL.md, lazy/eager modes, references/scripts/assets)
  schema.py      # JSON Schema generation from Python types (incl. Annotated/Field metadata)
  exceptions.py  # Framework exceptions (carry an optional ``.hint``)
  mcp.py         # Optional MCP client (requires mcp package)
  providers/     # LLM provider adapters (OpenAI, Anthropic, …)
  stores/        # Session and memory store implementations
  sandbox/       # Filesystem + process sandbox (Sandbox.local,
                 #   SandboxBackend/SandboxSession protocols)
  web/           # Optional FastAPI + SSE layer and bundled chat UI
                 #   (decoupled from core; only loaded when lovia[web] is used)
```

The codebase is three layers — **core** (everything outside `sandbox/` and
`web/`), **sandbox** (production fs+exec), **web** (HTTP/SSE/UI) — each
strictly downstream of the previous. Core never imports sandbox or web.

The `web/` module is fully decoupled from `lovia` core — nothing in `lovia`
imports `lovia.web` automatically, so agents that don't need HTTP keep their
lightweight dependency footprint.

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

We follow [Conventional Commits](https://www.conventionalcommits.org/) with a small set of types:

| Type | Use for |
| --- | --- |
| `feat`     | New user-visible capability |
| `fix`      | Bug fix |
| `docs`     | Docs / examples / README only |
| `refactor` | Internal change with no behaviour delta |
| `perf`     | Performance improvement |
| `test`     | Tests only |
| `chore`    | Build, deps, tooling |
| `revert`   | Reverts a previous commit |

Format:

```
<type>(<optional-scope>): <imperative summary, ≤72 chars>

<body — what & why, wrapped at ~72 cols>

<footer — BREAKING CHANGE: …, Refs: #123, …>
```

Examples:

```
feat(runner): per-call output_type override
fix(skills): reject path traversal in read_skill_file
docs: rewrite README around tools and DX
```

Co-author trailers are appreciated; AI-assisted commits should end with:

```
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```
