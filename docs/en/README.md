# lovia

**A lightweight, elegant, provider-neutral Agent framework for Python.** Start with one
Agent and typed Tools. Add streaming, persistence, context management,
plugins, a Workspace, or a Web UI only when the application needs them.

```bash
pip install lovia
```

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="Explain complex science with vivid, everyday analogies.",
    model="<model>",
)

result = agent.run_sync("Explain why the sky is blue in three sentences.")
print(result.output)
```

[Build your first Agent →](quickstart.md){ .md-button .md-button--primary }
[Try the Web UI →](web-ui.md){ .md-button }

## Why lovia

<div class="grid cards" markdown>

-   **A small, deliberate core**

    The core needs only an HTTP client and a data-validation library;
    integrations stay opt-in.

-   **A loop you can follow**

    Model Turns, Tool calls, retries, and failures follow one explicit path.
    Typed events and the canonical Transcript show exactly what happened.

-   **Context without rewritten history**

    Compaction changes only the next provider view. The complete record stays
    intact, while stable prompt prefixes keep provider caches useful.

-   **One extension model**

    Skills, MCP, Todo, and Memory use the same Plugin seam available to your
    own capabilities, instead of growing separate integration systems.

</div>

## Choose a path

| I want to… | Start here | Then add |
| --- | --- | --- |
| Build my first Agent | [Quickstart](quickstart.md) | [Agents](agents.md), [Running agents](running.md) |
| Connect a model or gateway | [Quickstart](quickstart.md#2-configure-a-model) | [Providers & models](providers.md) |
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
