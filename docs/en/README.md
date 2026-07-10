# lovia documentation

You do not need to read the whole manual before trying lovia. Run one agent
first, then jump to the page for the job in front of you.

## Start here

- **Run something first**: [Quickstart](quickstart.md)
- **Learn by copying code**: [Examples](../../examples/README.md)
- **Understand the run loop**: [Core concepts](concepts.md)

## Find the page you need

| I want to... | Read |
| --- | --- |
| Define agents, instructions, and variants | [Agents](agents.md) |
| Run agents and handle inputs, results, and errors | [Running agents](running.md) |
| Build streaming UIs or consume run events | [Streaming](streaming.md) |
| Let the model call Python functions | [Tools](tools.md) |
| Use built-in HTTP, search, and time tools | [Built-in tools](built-in-tools.md) |
| Return Pydantic objects or JSON | [Structured output](structured-output.md) |
| Configure models, compatible endpoints, or custom providers | [Providers & models](providers.md) |
| Compose agents with handoff or agent-as-tool | [Multi-agent](multi-agent.md) |
| Package reusable capabilities | [Plugins](plugins.md) |
| Load team knowledge, runbooks, or style guides | [Skills](skills.md) |
| Connect MCP server tools | [MCP](mcp.md) |
| Add long-term memory across conversations | [Memory](memory.md) |
| Persist chats, recover crashes, and make runs idempotent | [Sessions & checkpoints](sessions-and-checkpoints.md) |
| Manage long context and compaction | [Context management](context.md) |
| Add human approval for risky tools | [Human in the loop](human-in-the-loop.md) |
| Add input/output safety checks | [Guardrails](guardrails.md) |
| Configure retries, budgets, and cancellation | [Reliability](reliability.md) |
| Give an agent files and shell access | [Workspace](workspace.md) |
| Serve a chat UI or backend | [Web UI & server](web.md) |
| Bring my own frontend or service | [HTTP API](http-api.md) |
| Inspect logs, events, traces, and token usage | [Observability](observability.md) |
| Write deterministic offline tests | [Testing](testing.md) |
| Run behavioral evals and compare baselines | [Evals](eval.md) |

## Internals

[Architecture notes](../architecture.md) document how the framework itself is
built: module map, runner internals, and invariants. They are written for
contributors, but are useful whenever you want the design rationale.

---

中文文档：[docs/zh](../zh/README.md)。
