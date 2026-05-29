"""Coding agent with a local sandbox.

Run::

    python examples/23_sandbox_agent.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, Runner, events
from lovia.sandbox import Sandbox

load_dotenv()

MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


async def main() -> None:
    agent = Agent(
        name="coder",
        instructions="You are a careful coding agent. Inspect files before editing.",
        model=MODEL,
        sandbox=Sandbox.local(".", mode="coding"),
    )
    handle = Runner.stream(
        agent, "List the top-level Python files and explain their purpose.", max_turns=6
    )
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.ToolCallStarted):
            print(f"\n[tool] {ev.call.name}({ev.call.arguments})", flush=True)
        elif isinstance(ev, events.ToolCallCompleted):
            status = "error" if ev.is_error else "done"
            print(f"[tool:{status}] {ev.call.name}", flush=True)
        elif isinstance(ev, events.ApprovalRequired):
            print(f"\n[approval needed] {ev.call.name}({ev.call.arguments})")
            answer = input("approve? [y/N] ").strip().lower()
            ev.approve() if answer == "y" else ev.reject()

    result = await handle.result()
    print(f"\n\n[done, turns={result.turns}]")


if __name__ == "__main__":
    asyncio.run(main())
