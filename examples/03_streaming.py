"""Consume the event stream and print text deltas as they arrive."""

from __future__ import annotations
import os

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, events

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


async def main() -> None:
    agent = Agent(
        name="Storyteller",
        instructions="You write short, vivid stories.",
        model=MODEL,
    )
    # ``run_streamed`` returns a ``RunHandle`` that is both async-iterable
    # (yields events) and awaitable (resolves to the final ``RunResult``).
    handle = Runner.run_streamed(agent, "Tell me a 4-sentence story about a fox.")
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
    result = await handle.result()
    print(f"\n\n[done, turns={result.turns}, tokens={result.usage.output_tokens}]")


if __name__ == "__main__":
    asyncio.run(main())
