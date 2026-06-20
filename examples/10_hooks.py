"""Observe the run via ``AgentHooks``.

Hooks see every event (text deltas, tool calls, handoffs, ...) so you can
plug into Logfire, OpenTelemetry, or your own logger without changing the
agent definition.

Each handler is called as ``handler(event, ctx)``: it receives the event and
the run's live ``RunContext`` — the dynamic run state (``session_id``, the
active agent, cumulative usage, ...). Handlers that only need the event ignore
``ctx``.
"""

from __future__ import annotations

import asyncio
import os

from lovia import Agent, AgentHooks, RunContext, Runner, events, tool

from dotenv import load_dotenv

load_dotenv()


@tool
async def now() -> str:
    """Return a fake timestamp."""
    return "2025-01-01T00:00:00Z"


hooks = AgentHooks()


@hooks.on(events.ToolCallStarted)
async def log_tool_started(ev: events.ToolCallStarted, ctx: RunContext) -> None:
    print(f"[tool] -> {ev.call.name}({ev.call.arguments})")


@hooks.on(events.ToolCallCompleted)
async def log_tool_completed(ev: events.ToolCallCompleted, ctx: RunContext) -> None:
    print(f"[tool] <- {ev.call.name}: {ev.result}")


@hooks.on(events.RunCompleted)
async def log_done(ev: events.RunCompleted, ctx: RunContext) -> None:
    # Every handler also receives the run's live RunContext, so it can read
    # run-scoped state the event doesn't carry — here the session key.
    print(f"[done] session={ctx.session_id} usage={ev.result.usage}")


async def main() -> None:
    agent = Agent(
        name="Clock",
        instructions="Use the now tool to answer time questions.",
        model=os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4"),
        tools=[now],
        hooks=hooks,
    )
    result = await Runner.run(agent, "What time is it?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
