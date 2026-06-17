"""Agent backed by a real, remote-fetching MCP server, with streaming output.

Uses the official ``fetch`` MCP server
(https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) to pull
live data from a public, **no-auth** web API — here, current weather from
``wttr.in``. The MCP server itself needs no API key.

Prerequisites:
    * ``uv`` (provides ``uvx``): https://docs.astral.sh/uv/ — the ``fetch``
      server is downloaded and run on first use.
    * An OpenAI-compatible API key for the *model* (see ``.env``).

Run::

    python examples/26_mcp.py
    MCP_CITY="San Francisco" python examples/26_mcp.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from rich.console import Console

from lovia import Agent, Runner, events
from lovia.plugins.mcp import MCPServerStdio, MCP

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4")
CITY = os.getenv("MCP_CITY", "Shanghai")
console = Console()

# A real, remote-fetching MCP server: the official ``fetch`` server exposes a
# single ``fetch`` tool that retrieves a URL and returns its content. ``name``
# prefixes it as ``web__fetch``.
server = MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])


async def main() -> None:
    # Open the MCP connection once and reuse it across runs. (Passing the bare
    # ``server`` to ``MCP()`` instead would open/close it per run.)
    async with server.session() as conn:
        tools = conn.tools()
        console.print(
            f"[bold]MCP server connected[/bold] · tools: {[t.name for t in tools]}\n"
        )

        agent = Agent(
            name="weather",
            instructions=(
                "You answer questions by calling public web APIs with the fetch "
                "tool, then replying concisely from the response data."
            ),
            model=MODEL,
            plugins=[MCP(conn)],
        )

        url = f"https://wttr.in/{CITY}?format=j1"
        handle = Runner.stream(
            agent,
            f"Fetch {url} and report {CITY}'s current temperature, feels-like, "
            "and a one-line description of the weather.",
            max_turns=6,
        )
        async for ev in handle:
            if isinstance(ev, events.TextDelta):
                console.print(ev.delta, end="", soft_wrap=True, markup=False)
            elif isinstance(ev, events.ToolCallStarted):
                console.print(
                    f"\n[cyan]→ mcp[/cyan] {ev.call.name}({ev.call.arguments})"
                )
            elif isinstance(ev, events.ToolCallCompleted):
                style = "red" if ev.is_error else "green"
                state = "error" if ev.is_error else "ok"
                console.print(f"[{style}]← {state}[/{style}] {ev.call.name}")

        result = await handle.result()
        console.print(
            f"\n[dim]done · turns={result.turns} · "
            f"output tokens={result.usage.output_tokens}[/dim]"
        )


if __name__ == "__main__":
    asyncio.run(main())
