"""``now`` / ``sleep`` — the world's smallest clock."""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.builtins.time import now, sleep

load_dotenv()


async def main() -> None:
    agent = Agent(
        name="Clock",
        instructions="Use `now` for time questions. You may use `sleep` to wait.",
        model="openai:gpt-4o-mini",
        tools=[now, sleep],
    )
    result = await Runner.run(agent, "What time is it in Tokyo right now?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
