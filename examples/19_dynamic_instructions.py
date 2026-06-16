"""Dynamic system prompts via the @agent.system_prompt decorator.

Each registered fragment is rendered with the run context and appended to
the base instructions in registration order. ``Runner.run(..., extra_instructions=...)``
adds one more layer per call without mutating the agent.

Run::

    OPENAI_API_KEY=sk-... python examples/19_dynamic_instructions.py
"""

from __future__ import annotations
import os

import asyncio
from dataclasses import dataclass

from dotenv import load_dotenv

from lovia import Agent, Runner

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4")


@dataclass
class Deps:
    user_name: str
    locale: str


async def main() -> None:
    agent = Agent[Deps](
        name="Concierge",
        instructions="You are a friendly concierge.",
        model=MODEL,
    )

    @agent.system_prompt
    def greet(ctx) -> str:  # type: ignore[no-untyped-def]
        # TODO: ctx should be RunContext, not Deps; make the full context available in system prompts
        # return f"Address the user as {ctx.context.user_name}."
        return f"Address the user as {ctx.user_name}."

    @agent.system_prompt
    async def localise(ctx) -> str:  # type: ignore[no-untyped-def]
        return f"Reply in locale: {ctx.locale}."

    result = await Runner.run(
        agent,
        "Recommend one thing to do this evening.",
        context=Deps(user_name="Alex", locale="en-GB"),
        extra_instructions="Use under 25 words.",
    )
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
