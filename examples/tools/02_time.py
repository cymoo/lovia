"""``now`` / ``sleep`` — the world's smallest clock."""

from __future__ import annotations
import os

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.tools.time import now, sleep

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


async def main() -> None:
    agent = Agent(
        name="Clock",
        instructions="Use `now` for time questions. You may use `sleep` to wait.",
        model=MODEL,
        tools=[now, sleep],
    )
    result = await Runner.run(agent, "What time is it in Tokyo right now?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
