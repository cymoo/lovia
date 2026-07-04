"""Multi-agent triage with ``handoffs``.

The triage agent transfers control to a specialist, which continues in the
same run loop and sees the full transcript. List a plain ``Agent`` to accept
the derived ``transfer_to_<name>`` tool, or wrap it in :class:`Handoff` to
set the routing description the parent sees and to observe the transfer.

Run::

    python examples/07_handoff.py
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from dotenv import load_dotenv

from lovia import Agent, Handoff, RunContext, Runner

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )

billing = Agent(
    name="Billing",
    instructions="You handle invoices, refunds, and subscription questions.",
    model=MODEL,
)

support = Agent(
    name="Support",
    instructions="You debug product issues. Ask for reproduction steps.",
    model=MODEL,
)


def log_transfer(args: dict[str, Any], ctx: RunContext[Any]) -> None:
    print(f"[handoff] -> Support (reason: {args.get('reason')})")


triage = Agent(
    name="Triage",
    instructions=(
        "You are the first line of support. Route the user to the right "
        "specialist with a transfer tool instead of answering yourself."
    ),
    model=MODEL,
    handoffs=[
        billing,  # plain agent: tool name and description are derived
        Handoff(
            target=support,
            description="Product bugs, crashes, or error messages.",
            on_handoff=log_transfer,
        ),
    ],
)


async def main() -> None:
    for question in (
        "I was charged twice this month.",
        "Your app crashed when I clicked save.",
    ):
        result = await Runner.run(triage, question)
        print(f"Q: {question}")
        print(f"[resolved by {result.final_agent.name}]")
        print(result.output, "\n")


if __name__ == "__main__":
    asyncio.run(main())
