"""Fetching the web — ``web_fetch`` reads one URL for its content.

HTML becomes Markdown, so headings, links and images survive; JSON comes back
compacted, text as-is.

Run::

    python examples/tools/01_fetch.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, events, model_from_env
from lovia.tools import web_fetch

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


async def main() -> None:
    agent = Agent(
        name="Fetcher",
        instructions="Use web_fetch to read URLs. Summarise what you find.",
        model=MODEL,
        tools=[web_fetch()],
    )
    handle = Runner.stream(
        agent,
        "Read https://example.com and list every image it references, then "
        "fetch https://httpbin.org/json and tell me what's inside.",
    )
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.ToolCallStarted):
            print(f"\n[tool] {ev.call.name}", flush=True)
    result = await handle.result()
    print(f"\n\n[done, turns={result.turns}]")


if __name__ == "__main__":
    asyncio.run(main())
