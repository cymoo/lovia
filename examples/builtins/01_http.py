"""HTTP fetch — drop a single ``Tool`` into the agent and ask for a URL."""

from __future__ import annotations
import os

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.builtins.http import http_fetch

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


async def main() -> None:
    agent = Agent(
        name="Fetcher",
        instructions="Use http_fetch to retrieve URLs; summarise what you find.",
        model=MODEL,
        tools=[http_fetch],
    )
    result = await Runner.run(
        agent, "Fetch https://httpbin.org/json and tell me what's inside."
    )
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
