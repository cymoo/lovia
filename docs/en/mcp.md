# MCP

[Model Context Protocol](https://modelcontextprotocol.io) servers expose
tools your agent can call — filesystems, browsers, databases — without you
writing adapters. lovia's `MCP` plugin connects to servers, converts their
tools into ordinary [`Tool`](tools.md)s, and manages the connection
lifecycle per run.

```bash
pip install "lovia[mcp]"
```

```python
from lovia import Agent, model_from_env
from lovia.plugins.mcp import MCP, MCPServerStdio

agent = Agent(
    name="assistant",
    model=model_from_env(),
    plugins=[
        MCP(MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"]))
    ],
)
```

The `mcp` dependency is imported only when a connection is actually made,
so `lovia.plugins.mcp` is always safe to import; a missing package raises
`UserError` with the install hint at open time.

## Servers

Two transports, both frozen keyword-only configs:

```python
MCPServerStdio(command="uvx", args=["mcp-server-fetch"], env=None, name="web")
MCPServerStreamableHTTP(url="https://mcp.example.com/mcp", headers=None, name="api")
```

Shared options (on either config):

| Option | Default | Effect |
| --- | --- | --- |
| `name` | `None` | prefixes the server's tools: `name="web"` → `web__fetch` |
| `include_tools` / `exclude_tools` | `None` | allowlist / denylist of raw tool names |
| `needs_approval` | `False` | bool or predicate — gates every tool from this server through the normal [approval flow](human-in-the-loop.md) |
| `retries` / `timeout` / `max_output_chars` / `result_renderer` | `None` | per-tool policies, applied to each converted tool ([Tools](tools.md)) |
| `auto_reconnect` | `True` | reopen a dead connection and retry the call once |
| `close_after_run` | `True` | close the connection when the run ends |

`MCP(a, b, ...)` takes any number of servers; prefixes keep their tool
names from colliding (a clash is reported at run start like any other
duplicate tool name).

## Connection lifecycle

**Per-run (default).** Passing a server *config* means each run opens the
connection in plugin `setup()` and closes it at run end — stateless and
robust, at the cost of a subprocess/handshake per run.

**Persistent.** For many runs against one server, open a session yourself
and pass the live connection — `MCPServerLike` is satisfied by both configs
and connections:

```python
server = MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])

async with server.session() as conn:      # opened once
    agent = Agent(name="assistant", model=model_from_env(), plugins=[MCP(conn)])
    await Runner.run(agent, "Fetch https://example.com and summarize it.")
    await Runner.run(agent, "Now fetch the RFC index.")   # same connection
```

The run never closes a connection you opened (`close_after_run` is `False`
on a live `MCPConnection`); its lifetime is the `async with` block. One
persistent connection serves *sequential* runs — concurrent runs over a
single MCP session are unsupported; give each concurrent worker its own.

## How MCP tools behave

- Tool schemas are normalized into ordinary lovia `Tool`s
  (`normalize_schema` repairs loose ones) — they validate, render,
  truncate, and appear in [streaming events](streaming.md) exactly like
  native tools. A custom `result_renderer` on the server receives the raw
  `MCPToolResult`; the default rendering is `render_mcp_content`.
- **Failures split in two.** A protocol-level tool failure (the server
  answered with `isError`) is rendered back to the model as
  `[tool error] ...` so it can self-correct — not raised. A
  transport/connection failure raises `MCPError` (carrying `tool_name`),
  which ends the call like any tool exception.
- Tool *results* may carry resources: text is inlined; images/audio become
  size-stamped placeholders (never raw base64); resource links become
  `[resource link: uri]` lines.
- **Scope is deliberately tools-only.** MCP prompts, resource browsing,
  sampling, OAuth, and subscriptions are non-goals; the plugin does one
  thing.

## Sharp edges

- **`auto_reconnect` means at-least-once.** A call that died mid-flight is
  retried once on a fresh connection — a non-idempotent side effect
  (`create_ticket`) can happen twice. Set `auto_reconnect=False` on servers
  with mutating tools, and let the model see the error instead.
- **MCP tools run in parallel by default**, like every tool. A server
  whose tools mutate shared state has no barrier protection — wrap the
  risky ones via `include_tools` split across two server entries, or gate
  them with `needs_approval`.
- **`needs_approval` is per *server*, not per tool.** Splitting one server
  into two `MCPServer` entries (same command, different
  `include_tools`) is the idiom for "read tools free, write tools gated".
- **stdio servers inherit your process environment** unless you pass
  `env=`; there is no `cwd` option — launch via a wrapper script when a
  server needs a working directory.

## See also

- [Plugins](plugins.md) — the mechanism underneath
- [Tools](tools.md) — everything a converted MCP tool inherits
- [Human in the loop](human-in-the-loop.md) — gating server tools
- Example: [`24_mcp.py`](../../examples/24_mcp.py)
