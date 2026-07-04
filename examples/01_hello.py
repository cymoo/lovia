"""Minimal agent: one model call, no tools.

Run::

    cp .env.example .env   # once: set LOVIA_MODEL and your API key
    python examples/01_hello.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, Runner

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


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
