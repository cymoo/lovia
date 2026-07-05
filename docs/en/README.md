# lovia documentation

These guides go deep on one feature at a time. The [README](../../README.md)
is the short tour; the [examples](../../examples/README.md) are the runnable
tutorial track; this is the reference you come back to.

## Start here (in order)

1. **[Quickstart](quickstart.md)** — install, configure a model, and build a
   streaming, tool-using agent in about ten minutes.
2. **[Core concepts](concepts.md)** — the mental model: what a run actually
   does, and the five ideas the rest of the docs assume.
3. **[Examples](../../examples/README.md)** — thirty numbered, self-contained
   scripts. Read them in order for a guided tour; copy any one as a starting
   point.

Everything below is on-demand: read a guide when you need its feature.

## Guides

### Core

The everyday surface: defining agents, running them, and shaping what they
can do and say.

| Guide | Covers |
| --- | --- |
| [Agents](agents.md) | `Agent` fields, static and dynamic instructions, `clone()`, per-run dependencies |
| [Running agents](running.md) | `run` / `run_sync` / `stream`, inputs (including images and files), `RunResult`, errors |
| [Streaming](streaming.md) | the typed event catalog and how to build UIs on it |
| [Tools](tools.md) | `@tool`, schema derivation, parallel execution and barriers, retries, policies |
| [Built-in tools](built-in-tools.md) | HTTP fetch, web search, time, and the recall tool |
| [Structured output](structured-output.md) | `output_type`, validation, automatic repair |
| [Providers & models](providers.md) | model strings, OpenAI-compatible endpoints, fallback chains, custom providers, prompt caching, reasoning models |

### Composition

Building bigger behavior out of small agents and reusable capability bundles.

| Guide | Covers |
| --- | --- |
| [Multi-agent](multi-agent.md) | handoffs, agent-as-tool, and when to use which |
| [Plugins](plugins.md) | the one extension axis: what a plugin can contribute, and how to write one |
| [Skills](skills.md) | reusable instruction bundles with progressive disclosure |
| [MCP](mcp.md) | tools from Model Context Protocol servers |
| [Memory](memory.md) | long-term, cross-session memory: notes, archive, and recall |

### Production

The seams you wire into a real application: persistence, control, and limits.

| Guide | Covers |
| --- | --- |
| [Sessions & checkpoints](sessions-and-checkpoints.md) | multi-turn history, crash recovery, idempotent runs |
| [Context management](context.md) | compaction, the view/transcript split, custom context policies |
| [Human in the loop](human-in-the-loop.md) | tool approval in all its forms, and `ask_human` |
| [Guardrails](guardrails.md) | input and output checks that can stop a run |
| [Reliability](reliability.md) | retries, fallback, budgets, cancellation, mid-run steering |
| [Observability](observability.md) | hooks, tracing, logging, and usage accounting |
| [Workspace](workspace.md) | file and shell tools scoped by a permission policy |

### Serving & quality

Putting an agent in front of people, and keeping it honest.

| Guide | Covers |
| --- | --- |
| [Web UI & server](web.md) | `serve()`, the zero-config CLI, background runs, scheduling |
| [HTTP API](http-api.md) | the JSON + SSE endpoints, and bringing your own front-end |
| [Evals](eval.md) | declarative behavioral test suites with checks and an LLM judge |
| [Testing](testing.md) | deterministic offline tests with `ScriptedProvider` |

## Internals

[Architecture notes](../architecture.md) document how the framework itself is
built — module map, runner internals, invariants. They are written for
contributors, but make good reading when a guide's "why" isn't enough.

---

中文文档正在翻译中 — see [docs/zh](../zh/README.md).
