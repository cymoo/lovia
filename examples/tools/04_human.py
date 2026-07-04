"""Ask-a-human — the agent suspends until a human answers via the channel.

The operator side is one loop: ``async for q in channel.questions()`` yields
each question as the model asks it and ends when the channel is closed. In
production the loop body forwards to a chat UI / Slack bot / HTTP endpoint;
here we answer from the terminal.

Run::

    python examples/tools/04_human.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, model_from_env
from lovia.tools.human import HumanChannel, ask_human

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


async def main() -> None:
    channel = HumanChannel()

    async def operator() -> None:
        async for q in channel.questions():
            # input() blocks the loop; fine for a demo, use your UI in prod.
            channel.answer(q.id, input(f"\n[human] {q.question}\n> "))

    op = asyncio.create_task(operator())

    agent = Agent(
        name="Concierge",
        instructions="Ask the human for any missing detail before answering.",
        model=MODEL,
        tools=[ask_human(channel)],
    )
    result = await Runner.run(agent, "Book me a table somewhere nice tonight.")
    print(result.output)

    channel.close()  # ends the operator's questions() loop
    await op


if __name__ == "__main__":
    asyncio.run(main())
