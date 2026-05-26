"""Tool calling: the agent picks a tool, runs it, then summarizes the result."""

from __future__ import annotations

import asyncio

from lovia import Agent, Runner, tool


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
        model="openai:gpt-4o-mini",
        tools=[get_weather, add],
    )
    result = await Runner.run(agent, "What's the weather in Tokyo, and what is 2+2?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
