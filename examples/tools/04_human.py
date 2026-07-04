"""Ask-a-human — the agent suspends until a human answers via the channel.

In production wire the channel to a chat UI / Slack bot / HTTP endpoint;
here we answer from the terminal. ``channel.pending`` lists open questions;
``channel.answer(id, text)`` resolves one.

Run::

    python examples/tools/04_human.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.tools.human import HumanChannel, ask_human

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


async def main() -> None:
    channel = HumanChannel()

    async def answer_when_asked() -> None:
        # Poll until a question shows up, then answer it from stdin.
        while True:
            await asyncio.sleep(0.5)
            for q in channel.pending:
                ans = input(f"\n[human] {q.question}\n> ")
                channel.answer(q.id, ans)

    poll = asyncio.create_task(answer_when_asked())

    agent = Agent(
        name="Concierge",
        instructions="Ask the human for any missing detail before answering.",
        model=MODEL,
        tools=[ask_human(channel)],
    )
    result = await Runner.run(agent, "Book me a table somewhere nice tonight.")
    print(result.output)
    poll.cancel()


if __name__ == "__main__":
    asyncio.run(main())
