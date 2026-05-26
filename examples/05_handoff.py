"""Multi-agent triage with ``handoffs``.

The triage agent decides whether the user wants billing or technical help and
transfers control to the matching specialist. The conversation continues in
the same loop, so the specialist sees the full transcript.
"""

from __future__ import annotations

import asyncio

from lovia import Agent, Runner

billing = Agent(
    name="Billing",
    instructions="You handle invoices, refunds, and subscription questions.",
    model="openai:gpt-4o-mini",
)

support = Agent(
    name="Support",
    instructions="You debug product issues. Ask for reproduction steps.",
    model="openai:gpt-4o-mini",
)

triage = Agent(
    name="Triage",
    instructions=(
        "Route the user to the right specialist. "
        "If they mention money, transfer to Billing. "
        "If they mention bugs or errors, transfer to Support."
    ),
    model="openai:gpt-4o-mini",
    handoffs=[billing, support],
)


async def main() -> None:
    result = await Runner.run(triage, "Your app crashed when I clicked save.")
    print(f"Resolved by: {result.final_agent.name}")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
