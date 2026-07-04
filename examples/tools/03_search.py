"""Web search — defaults to DuckDuckGo via ``lovia[ddg]``.

Install with::

    pip install 'lovia[ddg]'

Or plug your own backend by implementing the :class:`WebSearch` Protocol
and passing it to ``web_search(my_backend)``.

Run::

    python examples/tools/03_search.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, Runner, events
from lovia.tools.search import duckduckgo_search

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


async def main() -> None:
    agent = Agent(
        name="Researcher",
        instructions="Use web_search to find sources; cite the top result.",
        model=MODEL,
        tools=[duckduckgo_search()],
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
