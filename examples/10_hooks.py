"""Observe the run via ``AgentHooks``.

Hooks see every event (text deltas, tool calls, handoffs, ...) so you can
plug into Logfire, OpenTelemetry, or your own logger without changing the
agent definition.
"""

from __future__ import annotations

import asyncio
import os

from lovia import Agent, AgentHooks, Runner, tool

from dotenv import load_dotenv

load_dotenv()


@tool
async def now() -> str:
    """Return a fake timestamp."""
    return "2025-01-01T00:00:00Z"


class PrintingHooks(AgentHooks):
    async def on_tool_call_started(self, call) -> None:
        print(f"[tool] -> {call.name}({call.arguments})")

    async def on_tool_call_completed(self, call, result, is_error) -> None:
        print(f"[tool] <- {call.name}: {result}")

    async def on_run_completed(self, result) -> None:
        print(f"[done] usage={result.usage}")


async def main() -> None:
    agent = Agent(
        name="Clock",
        instructions="Use the now tool to answer time questions.",
        model=os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini"),
        tools=[now],
        hooks=PrintingHooks(),
    )
    result = await Runner.run(agent, "What time is it?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
