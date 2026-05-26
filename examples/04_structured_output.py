"""Structured output via ``output_type``.

For OpenAI Chat Completions the runner sets ``response_format`` to a JSON
schema. For other providers it falls back to a synthetic ``final_output``
tool the model must call to finish.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from lovia import Agent, Runner


class WeatherReport(BaseModel):
    city: str
    temp_c: float
    summary: str


async def main() -> None:
    agent = Agent(
        name="Weather",
        instructions="Report the weather using the provided structure.",
        model="openai:gpt-4o-mini",
        output_type=WeatherReport,
    )
    result = await Runner.run(agent, "Make up a sunny report for Lisbon.")
    print(repr(result.output))
    print(result.output.summary)


if __name__ == "__main__":
    asyncio.run(main())
