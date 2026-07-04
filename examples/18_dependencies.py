"""Dependency injection: per-run deps flow into instructions *and* tools.

``Runner.run(..., context=deps)`` carries an arbitrary object through the
run. ``@agent.instruction`` fragments read it to shape the system prompt;
a tool receives it by annotating its first parameter as
``RunContext[Deps]`` (the runner injects the live context — the parameter
never appears in the tool's schema). ``extra_instructions`` stacks one more
per-call layer on top without mutating the agent.

Run::

    python examples/18_dependencies.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from dotenv import load_dotenv

from lovia import Agent, RunContext, Runner, tool, model_from_env

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


@dataclass
class Deps:
    user_name: str
    locale: str
    orders: dict[str, str]  # stands in for a real database handle


@tool
async def list_my_orders(ctx: RunContext[Deps]) -> str:
    """List the signed-in user's recent orders."""
    assert ctx.deps is not None
    return "\n".join(f"{oid}: {status}" for oid, status in ctx.deps.orders.items())


async def main() -> None:
    agent = Agent[Deps](
        name="Concierge",
        instructions="You are a store concierge.",
        model=MODEL,
        tools=[list_my_orders],
    )

    @agent.instruction
    def greet(ctx: RunContext[Deps]) -> str:
        assert ctx.deps is not None
        return f"Address the user as {ctx.deps.user_name}."

    @agent.instruction
    async def localise(ctx: RunContext[Deps]) -> str:
        assert ctx.deps is not None
        return f"Reply in locale: {ctx.deps.locale}."

    result = await Runner.run(
        agent,
        "Any updates on my orders?",
        context=Deps(
            user_name="Alex",
            locale="en-GB",
            orders={"A-1001": "shipped", "A-1002": "packing"},
        ),
        extra_instructions="Use under 40 words.",
    )
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
