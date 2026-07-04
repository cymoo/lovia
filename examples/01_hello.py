"""Minimal agent: one model call, no tools.

Run::

    cp .env.example .env   # once: set LOVIA_MODEL and your API key
    python examples/01_hello.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, model_from_env

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


async def main() -> None:
    agent = Agent(
        name="Greeter",
        instructions="You are a friendly assistant. Keep answers under 20 words.",
        model=MODEL,
    )
    result = await Runner.run(agent, "Say hello in three languages.")
    print(result.output)
    print(f"\n[turns={result.turns} usage={result.usage}]")

    # In a plain script or notebook you can skip asyncio entirely:
    #     result = agent.run_sync("Say hello in three languages.")


if __name__ == "__main__":
    asyncio.run(main())
