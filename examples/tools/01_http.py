"""HTTP fetch — drop a single ``Tool`` into the agent and ask for a URL.

Run::

    python examples/tools/01_http.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, events, model_from_env
from lovia.tools.http import http_request

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


async def main() -> None:
    agent = Agent(
        name="Fetcher",
        instructions="Use http_request to retrieve URLs; summarise what you find.",
        model=MODEL,
        tools=[http_request],
    )
    handle = Runner.stream(
        agent, "Fetch https://httpbin.org/json and tell me what's inside."
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
