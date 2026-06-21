"""Todo plugin: the agent externalizes a plan and keeps it visible every turn.

``Todo`` is the first :class:`~lovia.plugins.Plugin`. Attaching it adds a
``todo_write`` tool plus a per-turn ``<system-reminder>`` that re-shows the
current list to the model — without ever bloating the persisted transcript.

Observability: the structured list rides on each ``todo_write`` result, so a UI
can render progress by filtering ``ToolCallCompleted`` for the tool.
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, Runner, Todo, events
from lovia.plugins import TodoItem

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4")


async def main() -> None:
    agent = Agent(
        name="Builder",
        instructions="You complete multi-step engineering tasks carefully.",
        model=MODEL,
        plugins=[Todo()],
    )

    task = (
        "Scaffold a small TODO REST API: design the data model, add CRUD "
        "endpoints, write tests, and document it. You have no real tools here — "
        "just plan with the todo list and narrate each step as you 'finish' it."
    )

    # Stream events and surface the live todo list as it changes.
    async for event in Runner.stream(agent, task):
        if isinstance(event, events.ToolCallCompleted) and event.call.name == "todo_write":
            items: list[TodoItem] = event.result  # structured list[TodoItem]
            print("\n— todo list —")
            for t in items:
                box = {"pending": " ", "in_progress": "~", "completed": "x"}[t.status]
                label = t.active_form if (t.status == "in_progress" and t.active_form) else t.content
                print(f"  [{box}] {label}")


if __name__ == "__main__":
    asyncio.run(main())
