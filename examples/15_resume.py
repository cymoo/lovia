"""Checkpoint a run, then resume it later.

The runner snapshots the transcript at the end of every turn when a
:class:`Checkpointer` is provided. ``Runner.resume(...)`` rehydrates the
saved state and continues the loop — useful for long-running agents that
might be interrupted by a crash, deploy, or queue worker hand-off.

Note: the opaque ``RunContext.context`` value is not snapshotted; you
re-supply it on resume.
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, Runner, tool
from lovia.stores.sqlite_checkpointer import SQLiteCheckpointer

load_dotenv()


@tool
async def lookup(topic: str) -> str:
    """Pretend to look something up."""
    return f"facts about {topic}: 42"


async def main() -> None:
    cp = SQLiteCheckpointer("./resume_demo.sqlite")
    agent = Agent(
        name="researcher",
        instructions="Answer briefly using the lookup tool when helpful.",
        model=os.getenv("OPENAI_DEFAULT_MODEL", "deepseek-chat"),
        tools=[lookup],
    )

    run_id = "demo-run"

    # First call: snapshots are written at the end of each turn.
    result = await Runner.run(
        agent,
        "Use lookup('lovia') and summarise the result in one sentence.",
        checkpointer=cp,
        run_id=run_id,
    )
    print("first run:", result.output)

    # Imagine the process crashed before producing the final answer. Now a
    # fresh process resumes from the persisted snapshot. (Here it simply
    # picks up after the last turn — usually a no-op if the run completed,
    # but illustrative.)
    snap = await cp.load(run_id)
    print(f"snapshot: {len(snap.items)} items, {snap.turns} turns")

    resumed = await Runner.resume(agent, checkpointer=cp, run_id=run_id)
    print("resumed output:", resumed.output)


if __name__ == "__main__":
    asyncio.run(main())
