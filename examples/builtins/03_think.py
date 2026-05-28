"""``think`` — a free-form scratchpad the model can write to.

The tool's return value mirrors the input, so reasoning lands in the
transcript without any external side effect.
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.builtins.think import think

load_dotenv()


async def main() -> None:
    agent = Agent(
        name="Planner",
        instructions=(
            "Before answering, call `think` once to lay out your plan, "
            "then answer concisely."
        ),
        model="openai:gpt-4o-mini",
        tools=[think],
    )
    result = await Runner.run(
        agent, "I have 7 books and 3 shelves; how do I split them evenly-ish?"
    )
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
