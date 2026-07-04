"""Tool calling: typed Python functions the model can invoke.

``@tool`` derives the JSON schema from type hints, the docstring, and
``Annotated`` / ``Field`` metadata — there is no separate schema language.
Async tools are awaited; sync tools run in a worker thread. When the model
requests several calls in one turn they execute concurrently (a tool whose
side effects must not overlap opts out with ``@tool(parallel=False)``).

Run::

    python examples/02_tools.py
"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated, Literal

from dotenv import load_dotenv
from pydantic import Field

from lovia import Agent, Runner, enable_logging, tool

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )

enable_logging()  # Logs each tool call and its result to the console.


@tool
async def get_weather(city: str, units: Literal["c", "f"] = "c") -> str:
    """Return the current weather for ``city`` (fake data, no network)."""
    temp = 22 if units == "c" else 72
    return f"{city}: {temp}°{units.upper()}, partly cloudy"


RATES = {("USD", "JPY"): 155.30, ("USD", "EUR"): 0.92, ("EUR", "JPY"): 168.80}


@tool(strict=True)
def convert_currency(
    amount: Annotated[float, Field(description="Amount in the source currency", gt=0)],
    source: Annotated[str, "ISO 4217 code, e.g. 'USD'"],
    target: Annotated[str, "ISO 4217 code, e.g. 'JPY'"],
) -> str:
    """Convert between currencies at a fixed demo rate."""
    rate = RATES.get((source.upper(), target.upper()))
    if rate is None:
        # Raising inside a tool does not crash the run: the error text is
        # returned to the model as a failed tool result so it can react.
        raise ValueError(f"no rate for {source}->{target}")
    return f"{amount:.2f} {source.upper()} = {amount * rate:.2f} {target.upper()}"


# More per-tool policy, shown in later examples:
#   @tool(needs_approval=True)     -> 12_approval.py (human-in-the-loop)
#   @tool(retries=2, timeout=10)   -> 14_reliability.py
#   @tool(parallel=False)          -> serialize a side-effecting tool


async def main() -> None:
    agent = Agent(
        name="TravelHelper",
        instructions="Use tools when they help. Answer concisely.",
        model=MODEL,
        tools=[get_weather, convert_currency],
    )
    result = await Runner.run(
        agent, "What's the weather in Tokyo, and how much is 100 USD in JPY?"
    )
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
