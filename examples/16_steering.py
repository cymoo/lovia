"""Mid-run steering: inject user messages into a live run via a Mailbox.

Two ways to reach the same channel:

* **Caller-side** — pass ``mailbox=`` to :meth:`Runner.stream` and ``push()``
  from outside the run (another task, an HTTP handler). The runner drains the
  mailbox at each turn start and appends each item as a normal ``user``
  message, so the model sees it on its next call.
* **Run-side** — every run exposes its mailbox (the caller-supplied one, or a
  runner-created default) as ``ctx.mailbox``, so hooks and tools can steer the
  run they are part of — no plumbing required. Here a deadline hook tells the
  agent to wrap up after a fixed number of turns.

Timing: a push is seen at the next mailbox drain, which happens at each turn
start — *after* that turn's ``TurnStarted`` hooks fire. So a ``TurnStarted``
handler's push is already visible on that same turn's model call, while a push
from anywhere else (a tool, another event, outside the run) lands on the
following turn. Whatever is still queued when the run ends stays in a
caller-supplied mailbox for the next run; a runner-created default is gone.

Run::

    python examples/16_steering.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import (
    Agent,
    AgentHooks,
    Mailbox,
    RunContext,
    Runner,
    events,
    tool,
    model_from_env,
)

load_dotenv()

MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

CHAPTERS = [
    "A stranger arrives in the harbour town and asks for the lighthouse keeper.",
    "The keeper denies ever having met the stranger; the logbook says otherwise.",
    "A storm strands both men in the lighthouse; the logbook goes missing.",
    "The stranger's satchel holds letters addressed to the keeper's late wife.",
    "The keeper confesses: the wreck twenty years ago was no accident.",
    "At dawn the stranger sails away, leaving the letters and taking the truth.",
]


@tool
async def read_chapter(number: int) -> str:
    """Return the text of one chapter (1-based)."""
    if not 1 <= number <= len(CHAPTERS):
        return f"There is no chapter {number}; the book has {len(CHAPTERS)}."
    return f"Chapter {number}: {CHAPTERS[number - 1]}"


hooks = AgentHooks()


@hooks.on(events.TurnStarted)
def deadline(ev: events.TurnStarted, ctx: RunContext) -> None:
    # TurnStarted fires just before this turn's drain, so this push is seen on
    # this very model call — the nudge takes effect immediately.
    if ev.turn == 4:
        ctx.mailbox.push(
            "Deadline reached: stop reading further chapters and write your "
            "best summary from what you have."
        )


async def main() -> None:
    agent = Agent(
        name="book-reviewer",
        model=MODEL,
        instructions=(
            "You are reviewing a short novel, one chapter per turn: call "
            "read_chapter starting at 1, reflect briefly, and continue with "
            "the next chapter until you have read all six. Then produce a "
            "three-sentence review."
        ),
        tools=[read_chapter],
        hooks=hooks,
    )

    # Caller-side steering: hold the mailbox and push while the run is live.
    mailbox = Mailbox()

    async def impatient_reader() -> None:
        await asyncio.sleep(3)
        mailbox.push("A reader interrupts: please also name your favourite chapter.")

    asyncio.create_task(impatient_reader())

    handle = Runner.stream(agent, "Review the novel.", mailbox=mailbox)
    result = None
    async for ev in handle:
        if isinstance(ev, events.TurnStarted):
            print(f"--- turn {ev.turn} ---")
        elif isinstance(ev, events.UserMessageInjected):
            print(f">>> injected on turn {ev.turn}: {ev.content}")
        elif isinstance(ev, events.RunCompleted):
            result = ev.result

    assert result is not None
    print("\n=== review ===\n", result.output)
    print("\nturns:", result.turns, "usage:", result.usage)


if __name__ == "__main__":
    asyncio.run(main())
