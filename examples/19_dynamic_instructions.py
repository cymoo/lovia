"""Dynamic system prompts via the @agent.instruction decorator.

Each registered fragment receives the run's ``RunContext`` (reach your deps via
``ctx.deps``) and is appended to the base instructions in registration order.
``Runner.run(..., extra_instructions=...)`` adds one more layer per call without
mutating the agent.

Run::

    OPENAI_API_KEY=sk-... python examples/19_dynamic_instructions.py
"""

from __future__ import annotations
import os

import asyncio
from dataclasses import dataclass

from dotenv import load_dotenv

from lovia import Agent, RunContext, Runner

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.4" '
        'or "anthropic:claude-4-8-opus"'
    )


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

    @agent.instruction
    def greet(ctx: RunContext[Deps]) -> str:
        return f"Address the user as {ctx.deps.user_name}."

    @agent.instruction
    async def localise(ctx: RunContext[Deps]) -> str:
        return f"Reply in locale: {ctx.deps.locale}."

    result = await Runner.run(
        agent,
        "Recommend one thing to do this evening.",
        context=Deps(user_name="Alex", locale="en-GB"),
        extra_instructions="Use under 25 words.",
    )
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
