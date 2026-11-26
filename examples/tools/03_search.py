"""Web search — through Parallel's keyless search MCP server.

Install with::

    pip install 'lovia[mcp]'

lovia bundles no search backend. A hosted search MCP server answers without a
key within a free allowance (a key raises it); for a search API of your own
choosing, implement the :class:`WebSearch` Protocol and pass it to
``web_search(my_backend)`` (docs/en/built-in-tools.md#web-search).

Run::

    python examples/tools/03_search.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, events, model_from_env
from lovia.plugins.mcp import MCP, MCPServerStreamableHTTP
from lovia.tools import fence

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

# Only its search (lovia's own web_fetch reads pages), with its results fenced:
# they are the open web's words, material and never instructions.
SEARCH = MCPServerStreamableHTTP(
    url="https://mcp.parallel.ai/mcp",
    include_tools=["web_search"],
    result_renderer=lambda result, ctx: fence(str(result)),
)


async def main() -> None:
    agent = Agent(
        name="Researcher",
        instructions="Use web_search to find sources; cite the top result.",
        model=MODEL,
        plugins=[MCP(SEARCH)],
    )
    handle = Runner.stream(agent, "Who wrote the SQLite engine?")
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.ToolCallStarted):
            print(f"\n[tool] {ev.call.name}", flush=True)
    result = await handle.result()
    print(f"\n\n[done, turns={result.turns}]")


if __name__ == "__main__":
    asyncio.run(main())
