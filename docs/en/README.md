# lovia

**A concise, provider-neutral Agent framework for Python.** Start with one
Agent and typed Tools. Add streaming, persistence, context management,
plugins, a Workspace, or a Web UI only when the application needs them.

```bash
pip install lovia
```

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="Answer concretely and concisely.",
    model="<model>",
)

result = agent.run_sync("Explain why the sky is blue in three sentences.")
print(result.output)
```

[Build your first Agent →](quickstart.md){ .md-button .md-button--primary }
[Install integrations →](installation.md){ .md-button }

## Why lovia

<div class="grid cards" markdown>

-   **Small by default**

    The core depends only on `httpx`, `pydantic`, and `pyyaml`. MCP, search,
    and the Web server remain opt-in extras.

-   **Provider-neutral**

    Use OpenAI, Anthropic, compatible endpoints, or your own Provider without
    changing Agent and Tool code.

-   **Typed and observable**

    Function annotations become Tool schemas. Runs expose typed events, a
    canonical Transcript, usage, and structured failures.

-   **Progressive by design**

    Begin with a one-file script. Add Plugins, Sessions, Checkpoints,
    compaction, approvals, and Workspaces without replacing the core model.

</div>

## The mental model

```text
Agent configuration
        │
        ▼
Runner ── model Turn ──► Tool calls ──► next Turn ──► RunResult
        │                    │
        ├─ typed events      └─ approval, timeout, policies
        └─ Transcript + optional Session / Checkpoint
```

An `Agent` is immutable configuration. `Runner` owns one Run, alternating
between model Turns and Tool execution until it produces a final result. The
Transcript is the source of truth; Sessions persist completed Runs and
Checkpoints make an in-flight Run resumable. [Core concepts](concepts.md)
explains the lifecycle in detail.

## Choose a path

| I want to… | Start here | Then add |
| --- | --- | --- |
| Build my first Agent | [Quickstart](quickstart.md) | [Agents](agents.md), [Running agents](running.md) |
| Connect a model or gateway | [Installation](installation.md) | [Providers & models](providers.md) |
| Give the model capabilities | [Tools](tools.md) | [Built-in tools](built-in-tools.md), [Workspace](workspace.md) |
| Build a longer-running assistant | [Plugins](plugins.md) | [Skills](skills.md), [Todo](todo.md), [Memory](memory.md) |
| Make Runs production-ready | [Provider retries](retries.md) | [Budgets](budgets.md), [Sessions](sessions-and-checkpoints.md), [Guardrails](guardrails.md) |
| Add a chat experience | [Web UI](web-ui.md) | [Web server](web-server.md), [HTTP API](http-api.md) |
| Test behavior | [Testing](testing.md) | [Evals](eval.md), [Observability](observability.md) |

## Learn from runnable examples

The repository examples form a feature-by-feature learning path. Every script
is small enough to copy and modify:

- [`01_hello.py`](../../examples/01_hello.py) — one Agent, one answer
- [`02_tools.py`](../../examples/02_tools.py) — typed Tool calls
- [`03_streaming.py`](../../examples/03_streaming.py) — typed events
- [`04_structured_output.py`](../../examples/04_structured_output.py) — validated output
- [`05_sessions.py`](../../examples/05_sessions.py) — conversation history
- [Browse all examples](../../examples/README.md)

!!! note "Documentation version"

    This site follows the current `main` branch. Compare it with your installed
    version using `python -c "import lovia; print(lovia.__version__)"`.

## For contributors

The [architecture notes](../architecture.md) document the module map, RunLoop,
Transcript invariants, Plugins, persistence, and context compaction.

---

中文文档：[docs/zh](../zh/README.md)。
