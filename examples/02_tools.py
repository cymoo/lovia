"""Tool calling: the agent picks a tool, runs it, then summarizes the result."""

from __future__ import annotations
import os

import asyncio

from lovia import Agent, Runner, tool, enable_logging

from dotenv import load_dotenv

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.4" '
        'or "anthropic:claude-4-8-opus"'
    )

enable_logging()  # Logs all tool calls and their results to the console.


@tool
async def get_weather(city: str, units: str = "c") -> str:
    """Return the (fake) current weather for ``city``. ``units`` is "c" or "f"."""
    # In a real app this would be an HTTP call.
    return f"{city}: 22°{units.upper()}, partly cloudy"


@tool
def add(a: int, b: int) -> int:
    """Add two integers. Sync tools are fine; they run in a worker thread."""
    return a + b


async def main() -> None:
    agent = Agent(
        name="Helper",
        instructions="Use tools when they help. Answer concisely.",
        model=MODEL,
        tools=[get_weather, add],
    )
    result = await Runner.run(agent, "What's the weather in Tokyo, and what is 2+2?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
