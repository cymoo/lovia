"""Ask-a-human — the agent suspends until a human answers via the channel.

In production wire the channel to a chat UI / Slack bot / HTTP endpoint;
here we answer from the terminal.
"""

from __future__ import annotations
import os

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.tools.human import HumanChannel, ask_human

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4")


async def main() -> None:
    channel = HumanChannel()

    async def answer_when_asked() -> None:
        # Poll until a question shows up, then answer it from stdin.
        while True:
            await asyncio.sleep(0.5)
            for q in list(channel._pending.values()):
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
