"""Consume the event stream and print text as it arrives.

``Runner.stream`` returns a ``RunHandle`` that is both async-iterable
(yields typed events) and awaitable (resolves to the final ``RunResult``).
Every example that needs live output builds on this loop.

Run::

    python examples/03_streaming.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, Runner, events

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


async def main() -> None:
    agent = Agent(
        name="Storyteller",
        instructions="You write short, vivid stories.",
        model=MODEL,
    )
    handle = Runner.stream(agent, "Tell me a 4-sentence story about a fox.")
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.OutputDiscarded):
            # A transient mid-stream error discarded the partial output and the
            # turn restarts. Plain stdout can't unprint, so just mark the reset —
            # what follows replaces everything above it.
            print("\n[output reset — retrying]\n")
    result = await handle.result()
    print(
        f"\n\n[done · turns={result.turns} · "
        f"output tokens={result.usage.output_tokens}]"
    )


if __name__ == "__main__":
    asyncio.run(main())
