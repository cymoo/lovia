"""Structured output via ``output_type``.

For OpenAI Chat Completions the runner sets ``response_format`` to a JSON
schema. For other providers it falls back to a synthetic ``final_output``
tool the model must call to finish.
"""

from __future__ import annotations
import os

import asyncio

from pydantic import BaseModel

from lovia import Agent, Runner

from dotenv import load_dotenv

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


class WeatherReport(BaseModel):
    city: str
    temp_c: float
    summary: str


async def main() -> None:
    agent = Agent(
        name="Weather",
        instructions="Report the weather using the provided structure.",
        model=MODEL,
        output_type=WeatherReport,
    )
    result = await Runner.run(agent, "Make up a sunny report for Lisbon.")
    print(repr(result.output))
    print(result.output.summary)


if __name__ == "__main__":
    asyncio.run(main())
