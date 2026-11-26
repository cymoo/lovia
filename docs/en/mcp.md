# MCP

[Model Context Protocol](https://modelcontextprotocol.io) (MCP) lets an Agent
use tools supplied by servers, such as filesystems, browsers, and databases.
The `MCP` plugin connects to a server and converts those capabilities into
ordinary [`Tool`](tools.md)s.

```bash
pip install "lovia[mcp]"
```

The plugin supports the MCP Python SDK 1.18+ and 2.x.

```python
from lovia import Agent
from lovia.plugins.mcp import MCP, MCPServerStdio

agent = Agent(
    name="assistant",
    model="<model>",
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
MCPServerStdio(command="uvx", args=["mcp-server-fetch"], env=None, cwd=None, name="web")
MCPServerStreamableHTTP(url="https://mcp.example.com/mcp", headers=None, trust_env=None, name="api")
```

An HTTP server's client takes lovia's [proxy and TLS settings](providers.md#networking-timeouts-proxies-tls):
proxies off unless `LOVIA_HTTP_TRUST_ENV=1`, and `trust_env=True` / `False`
decides for one server — say, a public server reachable only through the
office proxy, beside intranet ones that must not use it.

Shared options (on either config):

| Option | Default | Effect |
| --- | --- | --- |
| `name` | `None` | prefixes the server's tools: `name="web"` → `web__fetch` |
| `include_tools` / `exclude_tools` | `None` | allowlist / denylist of raw tool names |
| `permission` | `"allow"` | `"ask"` / `"deny"` / `Verdict` or a predicate — judges every tool from this server through the normal [approval flow](tools.md#tool-approval) |
| `retries` / `timeout` / `max_output_chars` / `result_renderer` | `None` | per-tool policies, applied to each converted tool ([Tools](tools.md)) |
| `auto_reconnect` | `True` | reopen a dead connection and retry the call once |
| `close_after_run` | `True` | close the connection when the run ends |

`MCP(a, b, ...)` takes any number of servers; prefixes keep their tool
names from colliding (a clash is reported at run start like any other
duplicate tool name).

## Web research with Parallel

[Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp)
provides web search and page fetching over Streamable HTTP, with no account or
API key required for its free tier. Anonymous search uses Fast mode; free usage
has rate limits. Install `lovia[mcp]` as above and configure your
[model environment variables](providers.md), then run this
as a Python script:

```python
from lovia import Agent, model_from_env
from lovia.plugins.mcp import MCP, MCPServerStreamableHTTP

server = MCPServerStreamableHTTP(
    name="parallel",
    url="https://search.parallel.ai/mcp",
    headers={"User-Agent": "lovia-parallel-mcp"},
)
agent = Agent(
    name="researcher",
    model=model_from_env(),
    instructions="Search for sources, fetch relevant pages, and cite their URLs.",
    plugins=[MCP(server)],
)
result = agent.run_sync(
    "Find the latest stable Python release and summarize its official release notes."
)
print(result.output)
```

The plugin exposes `parallel__web_search` and `parallel__web_fetch`. Search finds
candidate sources; fetch reads selected URLs. Only agents given this plugin use
the server. From async code, use `await Runner.run(agent, ...)` instead of
`run_sync()`.

## A browser for a coding agent

An agent that edits a web app can look at the page it just changed.
[Playwright MCP](https://github.com/microsoft/playwright-mcp) drives the
browser; lovia needs nothing beyond the server entry:

```python
browser = MCPServerStdio(
    name="browser",
    command="npx",
    args=["-y", "@playwright/mcp@latest", "--headless", "--isolated"],
)
agent = Agent(name="coder", model="<model>", plugins=[MCP(browser)])
```

In `lovia web` the same entry goes into
[Settings → MCP](web-ui.md#mcp-servers) or the project's `.mcp.json`:

```json
{
  "mcpServers": {
    "browser": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest", "--headless", "--isolated"]
    }
  }
}
```

With a [workspace](workspace.md), the agent starts the dev server with
`shell(background=true)`, opens it with `browser__browser_navigate`, and then
has two ways to look:

- `browser__browser_snapshot` returns the page as text: an accessibility tree
  whose element refs are what `browser__browser_click` takes. It costs far
  fewer tokens than a picture, so it is the one to act on.
- `browser__browser_take_screenshot` returns the rendered page as an image the
  model sees (below), for layout and styling.

`browser__browser_console_messages` and `browser__browser_network_requests`
report what broke. A page is outside content: what it says is data for the
agent. Which calls ask first is the server's `permission`; `lovia web`
[asks before each one](web-ui.md#mcp-servers) outside Full access.

The browser lives as long as the connection. A per-run connection, which is
what `lovia web` uses, starts a new browser for every run, so the agent opens
the page again in each message.

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
    agent = Agent(name="assistant", model="<model>", plugins=[MCP(conn)])
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
- Tool *results* may carry resources: text is inlined; audio and binary
  resources become size-stamped placeholders (never raw base64); resource
  links become `[resource link: uri]` lines.
- **Images reach the model as images.** A jpeg, png, gif or webp block whose
  base64 is valid and at most 5 MB (about 3.7 MB of image) is handed over as
  an [`ImagePart`](tools.md#returning-images), in order with the result's
  text. Any other image, and every image for a model without vision, is a
  size-stamped placeholder. Such a result is content
  parts, which no renderer formats. A server with a custom `result_renderer`
  is the exception: its renderer still gets the raw `MCPToolResult` and
  returns text. A result flagged `isError` is text too.
- **Scope is deliberately tools-only.** MCP prompts, resource browsing,
  sampling, OAuth, and subscriptions are non-goals; the plugin does one
  thing.

## Deferring large servers

A server with dozens of tools costs its full schema catalog in every prompt,
even when the model uses none of them. Set `defer=True` and the server's
tools stay out of the tool list; the agent instead gets one shared pair —
`search_mcp_tools` (keyword lookup returning name, description, and
parameters schema) and `call_mcp_tool` (invoke by exact name) — plus one
system-prompt line naming the deferred tools, so the model knows they exist
without paying for their schemas:

```python
plugins=[MCP(
    MCPServerStdio(command="...", name="github", defer=True),  # 60 tools
    MCPServerStdio(command="..."),                             # 3 tools, direct
)]
```

Deferral changes *when schemas enter the context*, not how tools run: a
deferred call is fully delegated, so the server's `permission`,
`retries`, `timeout`, `result_renderer`, and `max_output_chars` apply
exactly as if the tool were exposed directly. Names must stay unambiguous —
a deferred name colliding with another deferred or exposed tool in the same
plugin raises `UserError` at setup (give servers a `name=` prefix).

## Sharp edges

- **`auto_reconnect` means at-least-once.** A call that died mid-flight is
  retried once on a fresh connection — a non-idempotent side effect
  (`create_ticket`) can happen twice. Set `auto_reconnect=False` on servers
  with mutating tools, and let the model see the error instead — it says
  the call may or may not have taken effect.
- **MCP tools run in parallel by default**, like every tool. A server
  whose tools mutate shared state has no barrier protection — wrap the
  risky ones via `include_tools` split across two server entries, or gate
  them with `permission="ask"`.
- **`permission` is per *server*, not per tool.** Splitting one server
  into two `MCPServer` entries (same command, different
  `include_tools`) is the idiom for "read tools free, write tools gated".
- **stdio servers inherit your process environment** unless you pass
  `env=`, and start in the current directory unless you pass `cwd=`.

## See also

- [Plugins](plugins.md) — the mechanism underneath
- [Tools](tools.md) — everything a converted MCP tool inherits
- [Tool approval](tools.md#tool-approval) — gating server tools
- [Web UI → MCP servers](web-ui.md#mcp-servers) — the same servers from
  `lovia web`'s Settings, no code
- Example: [`24_mcp.py`](../../examples/24_mcp.py)
