"""Structured output: get a validated Python object instead of prose.

Set ``output_type`` on the agent (or per call) and ``result.output`` is an
instance of that type. Providers with native JSON-schema support use it;
for the rest the runner falls back to a synthetic ``final_output`` tool.

Run::

    python examples/04_structured_output.py
"""

from __future__ import annotations

import asyncio
import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel

from lovia import Agent, Runner

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


class WeatherReport(BaseModel):
    city: str
    temp_c: float
    summary: str


class Alert(BaseModel):
    severity: Literal["info", "watch", "warning"]
    headline: str
    advice: str


async def main() -> None:
    agent = Agent(
        name="Weather",
        instructions="Report the weather using the provided structure.",
        model=MODEL,
        output_type=WeatherReport,
    )

    # 1. The agent's default output type.
    result = await Runner.run(agent, "Make up a sunny report for Lisbon.")
    report: WeatherReport = result.output
    print(repr(report))
    print(report.summary)

    # 2. Override the output type for one call — no agent clone needed.
    alert_result = await Runner.run(
        agent,
        "A typhoon is approaching Okinawa. Issue an alert.",
        output_type=Alert,
    )
    alert: Alert = alert_result.output
    print(f"\n[{alert.severity}] {alert.headline}\n{alert.advice}")


if __name__ == "__main__":
    asyncio.run(main())
