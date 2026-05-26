"""Consume the event stream and print text deltas as they arrive."""

from __future__ import annotations

import asyncio

from lovia import Agent, Runner, events


async def main() -> None:
    agent = Agent(
        name="Storyteller",
        instructions="You write short, vivid stories.",
        model="openai:gpt-4o-mini",
    )
    async for ev in Runner.run_stream(agent, "Tell me a 4-sentence story about a fox."):
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.RunCompleted):
            print(f"\n\n[done, turns={ev.result.turns}]")


if __name__ == "__main__":
    asyncio.run(main())
