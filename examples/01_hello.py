"""Minimal "hello world" agent backed by OpenAI Chat Completions.

Run::

    OPENAI_API_KEY=sk-... python examples/01_hello.py
"""

from __future__ import annotations

import asyncio

from lovia import Agent, Runner

from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    agent = Agent(
        name="Greeter",
        instructions="You are a friendly assistant. Keep answers under 20 words.",
        model="deepseek-v4-pro",
    )
    result = await Runner.run(agent, "Say hello in three languages.")
    print(result.output)
    print(f"\n[turns={result.turns} usage={result.usage}]")


if __name__ == "__main__":
    asyncio.run(main())
