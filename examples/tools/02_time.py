"""``now`` / ``sleep`` — the world's smallest clock.

Run::

    python examples/tools/02_time.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, model_from_env
from lovia.tools.time import now, sleep

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


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
