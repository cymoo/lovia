"""Run a lovia agent inside a Prefect flow.

Run::

    pip install -e .[examples]
    python examples/24_prefect.py
"""

from __future__ import annotations

import asyncio
import os

# Disable Prefect telemetry before importing prefect to avoid SQLite heartbeat errors.
os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("PREFECT_SERVER_ANALYTICS_ENABLED", "false")

from dotenv import load_dotenv
from prefect import flow, task
from rich.console import Console
from rich.panel import Panel

from lovia import Agent, Runner

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")
console = Console()


@task(retries=1, retry_delay_seconds=2)
async def ask_agent(topic: str) -> str:
    agent = Agent(
        name="planner",
        instructions="You turn rough goals into short, actionable plans.",
        model=MODEL,
    )
    result = await Runner.run(agent, f"Create a 3-step plan for: {topic}")
    return str(result.output)


@flow(name="lovia-agent-plan")
async def agent_plan_flow(topic: str = "launch a tiny Python package") -> str:
    return await ask_agent(topic)


async def main() -> None:
    plan = await agent_plan_flow()
    console.print(Panel(plan, title="Prefect + lovia", border_style="green"))


if __name__ == "__main__":
    asyncio.run(main())
