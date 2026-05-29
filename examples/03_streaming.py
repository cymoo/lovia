"""Consume the event stream and print text deltas as they arrive."""

from __future__ import annotations
import os

import asyncio

from dotenv import load_dotenv
from rich.console import Console

from lovia import Agent, Runner, events

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")
console = Console()


async def main() -> None:
    agent = Agent(
        name="Storyteller",
        instructions="You write short, vivid stories.",
        model=MODEL,
    )
    # ``stream`` returns a ``RunHandle`` that is both async-iterable
    # (yields events) and awaitable (resolves to the final ``RunResult``).
    handle = Runner.stream(agent, "Tell me a 4-sentence story about a fox.")
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            console.print(ev.delta, end="", soft_wrap=True, markup=False)
    result = await handle.result()
    console.print(
        f"\n[dim]done · turns={result.turns} · output tokens={result.usage.output_tokens}[/dim]"
    )


if __name__ == "__main__":
    asyncio.run(main())
