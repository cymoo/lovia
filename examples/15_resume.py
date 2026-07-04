"""Checkpoint a run, kill it mid-flight, then resume where it stopped.

With a :class:`Checkpointer` the runner snapshots the transcript at the end
of every turn. Re-issuing the same call under the same ``run_id`` continues
from the last snapshot instead of starting over — built for crashes,
deploys, and queue-worker hand-offs.

The demo fetches three chapters, one tool call per turn. Phase 1 "crashes"
(``handle.cancel()``) after the first chapter is safely checkpointed; the
stream ends with a ``RunFailed`` event — iteration never raises — and
``handle.result()`` raises ``RunCancelled``. Phase 2 re-issues the identical
call and finishes the job without refetching chapter 1.

Run::

    python examples/15_resume.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from lovia import (
    Agent,
    CheckpointOptions,
    RunCancelled,
    Runner,
    SQLiteCheckpointer,
    events,
    model_from_env,
    tool,
)

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

CHAPTERS = {
    1: "A stranger arrives in the harbour town at dusk.",
    2: "The lighthouse logbook contradicts the keeper's story.",
    3: "At dawn the stranger sails away with the truth.",
}
fetched: list[int] = []  # which chapters the *current* phase fetched


@tool
async def fetch_chapter(number: int) -> str:
    """Fetch one chapter of the novel (a slow upstream in real life)."""
    fetched.append(number)
    return f"Chapter {number}: {CHAPTERS[number]}"


agent = Agent(
    name="reader",
    instructions=(
        "Fetch chapters 1 to 3 with fetch_chapter — exactly one call per "
        "turn, in order. After chapter 3, summarise the story in one sentence."
    ),
    model=MODEL,
    tools=[fetch_chapter],
)

TASK = "Read the novel and summarise it."


async def main() -> None:
    Path("tmp").mkdir(exist_ok=True)
    cp = SQLiteCheckpointer("tmp/resume_demo.sqlite")
    run_id = "novel-summary"
    await cp.delete(run_id)  # clean slate so the demo is repeatable

    # ── Phase 1: the run dies mid-flight ─────────────────────────────────
    handle = Runner.stream(agent, TASK, checkpoint=CheckpointOptions(cp, run_id))
    async for ev in handle:
        if isinstance(ev, events.TurnStarted) and ev.turn == 2:
            # Turn 1 (chapter 1) is snapshotted; "crash" before turn 2 acts.
            handle.cancel("simulated crash")
    try:
        await handle.result()
    except RunCancelled:
        print(f"phase 1: crashed after fetching chapters {fetched}")

    snap = await cp.load(run_id)
    assert snap is not None
    print(f"phase 1: snapshot has {snap.turns} turn(s), {len(snap.entries)} entries")

    # ── Phase 2: a fresh worker re-issues the identical call ─────────────
    fetched.clear()
    result = await Runner.run(
        agent,
        TASK,  # same input, same run_id -> resumes, does not restart
        checkpoint=CheckpointOptions(cp, run_id, delete_on_success=True),
    )
    print(f"phase 2: resumed and fetched only chapters {fetched}")
    print("summary:", result.output)


if __name__ == "__main__":
    asyncio.run(main())
