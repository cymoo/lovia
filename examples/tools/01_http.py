"""HTTP fetch — drop a single ``Tool`` into the agent and ask for a URL."""

from __future__ import annotations
import os

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, events
from lovia.tools.http import http_fetch

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


async def main() -> None:
    agent = Agent(
        name="Fetcher",
        instructions="Use http_fetch to retrieve URLs; summarise what you find.",
        model=MODEL,
        tools=[http_fetch],
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
