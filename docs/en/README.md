# lovia

**A concise, provider-neutral agent framework for Python.** Build one agent,
give it typed tools, then add persistence, plugins, a workspace, or a web UI
only when the application needs them.

```bash
pip install lovia
```

<div class="grid cards" markdown>

-   **Run your first agent**

    Configure a model and get a working answer in a few minutes.

    [Start the quickstart →](quickstart.md)

-   **Understand the runtime**

    Learn how agents, runs, turns, tools, transcripts, and plugins fit
    together.

    [Read the core concepts →](concepts.md)

-   **Build from examples**

    Copy small, self-contained scripts covering the framework feature by
    feature.

    [Browse runnable examples →](../../examples/README.md)

-   **Prepare for production**

    Add reliability limits, persistence, safety gates, observability, and
    deployment boundaries.

    [Open the deployment checklist →](deployment.md)

</div>

## Choose a guide

| Goal | Guide |
| --- | --- |
| Define and run an agent | [Agents](agents.md) · [Running agents](running.md) |
| Configure a model or endpoint | [Installation & model setup](installation.md) · [Providers & models](providers.md) |
| Give the model capabilities | [Tools](tools.md) · [Workspace](workspace.md) · [Multi-agent](multi-agent.md) |
| Extend an agent | [Plugins](plugins.md) · [Skills](skills.md) · [MCP](mcp.md) · [Memory](memory.md) |
| Keep runs safe and durable | [Reliability](reliability.md) · [Sessions](sessions-and-checkpoints.md) · [Human approval](human-in-the-loop.md) |
| Serve and verify an application | [Web server](web.md) · [HTTP API](http-api.md) · [Testing](testing.md) · [Evals](eval.md) |

Looking for an exact type, exception, or common failure? Use the
[API reference](api-reference.md) or [troubleshooting guide](troubleshooting.md).

!!! note "Documentation version"

    This site follows the current `main` branch. Check your installed version
    with `python -c "import lovia; print(lovia.__version__)"` when comparing
    behavior with the source documentation.

## For contributors

The [architecture notes](../architecture.md) explain the module map, runner
internals, and invariants for people changing lovia itself.

---

中文文档：[docs/zh](../zh/README.md)。
