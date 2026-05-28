"""Web search — defaults to DuckDuckGo via ``lovia[tools]``.

Install with::

    pip install 'lovia[tools]'

Or plug your own backend by implementing the :class:`WebSearch` Protocol
and passing it to ``web_search(my_backend)``.
"""

from __future__ import annotations
import os

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.builtins.search import duckduckgo_search_tool

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


async def main() -> None:
    agent = Agent(
        name="Researcher",
        instructions="Use web_search to find sources; cite the top result.",
        model=MODEL,
        tools=[duckduckgo_search_tool()],
    )
    result = await Runner.run(agent, "Who wrote the SQLite engine?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
