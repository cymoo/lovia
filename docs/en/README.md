# lovia

lovia is a lightweight Python agent framework. Start with one `Agent` and a few
lines of Python; add tools, streaming, persistence, a workspace, or a web UI
only when you need them.

```bash
pip install lovia
```

Configure an endpoint in [Quickstart: configure a model](quickstart.md#2-configure-a-model),
then replace `<model>` below with a model name exposed by that endpoint.

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="Answer clearly and concretely.",
    model="<model>",
)

print(agent.run_sync("Why is the sky blue?").output)
```

[Complete your first run →](quickstart.md){ .md-button .md-button--primary }
[Open the Web UI →](web-ui.md){ .md-button }

## Start from your goal

| I want to… | Start with | Then explore |
| --- | --- | --- |
| Run my first agent | [Quickstart](quickstart.md) | [Core concepts](concepts.md) |
| Connect a model or gateway | [Configure a model](quickstart.md#2-configure-a-model) | [Providers & models](providers.md) |
| Let the model call code | [Tools](tools.md) | [Built-in tools](built-in-tools.md) |
| Read files or run commands | [Workspace](workspace.md) | [Tool approvals](tools.md#tool-approval) |
| Preserve conversations or resume work | [Sessions & checkpoints](sessions-and-checkpoints.md) | [Context management](context.md) |
| Compose multiple agents | [Multi-agent](multi-agent.md) | [Plugins](plugins.md) |
| Build a chat application | [Web UI](web-ui.md) | [Web server](web-server.md) · [HTTP API](http-api.md) |
| Test agent behavior | [Testing](testing.md) | [Evals](eval.md) · [Observability](observability.md) |

## lovia's tradeoffs

- The core depends only on `httpx`, `pydantic`, and `pyyaml`; integrations are optional.
- `Agent` holds configuration only; the run loop owns mutable state for each run.
- Context compaction changes the model view, not this run's transcript;
  configured limits may still truncate oversized Tool output.
- Skills, MCP, Todo, Memory, and custom capabilities share the plugin extension point.

## Learn from examples

The examples are ordered by difficulty and run independently:

- [`01_hello.py`](../../examples/01_hello.py): first run
- [`02_tools.py`](../../examples/02_tools.py): tool calls
- [`03_streaming.py`](../../examples/03_streaming.py): streaming events
- [`04_structured_output.py`](../../examples/04_structured_output.py): structured output
- [`05_sessions.py`](../../examples/05_sessions.py): conversation history
- [All examples](../../examples/README.md)

!!! note "Version"

    This site follows `main` and may be newer than your installed package. Run
    `python -c "import lovia; print(lovia.__version__)"` to check your local version.

Read the [architecture notes](../architecture.md) before changing framework
internals.

中文文档：[docs/zh](../zh/README.md)。
