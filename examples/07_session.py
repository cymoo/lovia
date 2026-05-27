"""Multi-turn conversation persisted with ``SQLiteSession``.

Each ``Runner.run`` call loads the prior transcript for ``session_id`` and
appends the new turns. Use the same ``session_id`` across processes to resume.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from lovia import Agent, Runner
from lovia.stores import SQLiteSession

from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    session = SQLiteSession(Path("/tmp/lovia_demo.db"))
    agent = Agent(
        name="Companion",
        instructions="You remember the user across turns.",
        model=os.getenv("OPENAI_DEFAULT_MODEL", "deepseek-chat"),
    )

    r1 = await Runner.run(agent, "Hi, I'm Mei.", session=session, session_id="user-mei")
    print("A:", r1.output)

    r2 = await Runner.run(
        agent, "What's my name?", session=session, session_id="user-mei"
    )
    print("A:", r2.output)


if __name__ == "__main__":
    asyncio.run(main())
