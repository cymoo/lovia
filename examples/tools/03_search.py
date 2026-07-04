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

from dotenv import load_dotenv

from lovia import Agent, Runner, events, model_from_env
from lovia.tools.search import duckduckgo_search

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


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
