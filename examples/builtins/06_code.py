"""Python runner — execute short snippets in a subprocess.

Defaults to ``needs_approval=True``. Pass ``needs_approval=False`` to make
it run unattended (only in trusted contexts).
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.builtins.code import PythonRunner

load_dotenv()


async def main() -> None:
    py = PythonRunner(needs_approval=False)
    agent = Agent(
        name="Calc",
        instructions="Use python to compute when needed; print the answer.",
        model="openai:gpt-4o-mini",
        tools=[py.tool()],
    )
    result = await Runner.run(agent, "What is the 20th Fibonacci number?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
