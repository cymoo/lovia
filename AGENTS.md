# AGENTS.md

Instructions for AI coding assistants working on this repository.

## Project overview

**lovia** is a lightweight, provider-neutral agent framework for Python. Core is under ~2000 lines; hard dependencies are only `httpx` and `pydantic`.

## Setup

```bash
pip install -e .[dev]
# Optional MCP support
pip install -e .[mcp]
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
  tools.py       # @tool decorator and Tool type
  messages.py    # ChatMessage, ToolCall, Usage types
  events.py      # Streaming event types
  output.py      # Structured output handling
  handoff.py     # Handoff between agents
  hooks.py       # AgentHooks base class
  session.py     # Session protocol
  skills.py      # Skill / SkillCatalog (SKILL.md lazy loading)
  schema.py      # JSON Schema generation from Python types
  exceptions.py  # Framework exceptions
  mcp.py         # Optional MCP client (requires mcp package)
  providers/     # LLM provider adapters (OpenAI, Anthropic, …)
  stores/        # Session and memory store implementations
```

## Conventions

- **Async-only** public API. All runner methods are `async`; use `asyncio.run()` in scripts.
- **Pydantic v2** for data models. Prefer `model_fields`, `model_validate`, etc.
- **Type annotations** on all public functions and classes.
- Keep the core minimal — avoid adding new hard dependencies without a strong reason.
- Provider adapters live under `lovia/providers/`; each adapter translates between the lovia message format and the vendor API.
- Tests live under `tests/`; use `pytest-asyncio` (already configured with `asyncio_mode = auto`).
- Follow `ruff` rules for formatting and linting. Do not add `# noqa` suppressions without a comment explaining why.
