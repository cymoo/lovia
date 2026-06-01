"""Minimal "hello world" agent backed by OpenAI Chat Completions.

Run::

    OPENAI_API_KEY=sk-... python examples/01_hello.py
"""

from __future__ import annotations
import os

import asyncio

from lovia import Agent, Runner

from dotenv import load_dotenv

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4")


async def main() -> None:
    agent = Agent(
        name="Greeter",
        instructions="You are a friendly assistant. Keep answers under 20 words.",
        model=MODEL,
    )
    result = await Runner.run(agent, "Say hello in three languages.")
    print(result.output)
    print(f"\n[turns={result.turns} usage={result.usage}]")


if __name__ == "__main__":
    asyncio.run(main())
