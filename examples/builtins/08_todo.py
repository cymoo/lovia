"""Todo list — share an in-memory state with the agent.

The model uses ``add_todo`` / ``update_todo`` / ``list_todos`` to plan
its work; you can render the same ``TodoList`` for the user at any time.
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.builtins.todo import TodoList, todo_tools

load_dotenv()


async def main() -> None:
    todos = TodoList()
    agent = Agent(
        name="Planner",
        instructions=(
            "Break the user request into todos using add_todo, then "
            "mark each one done as you go."
        ),
        model="openai:gpt-4o-mini",
        tools=todo_tools(todos),
    )
    result = await Runner.run(
        agent, "Plan how to write a blog post about caching, then execute the plan."
    )
    print(result.output)
    print("\n--- Final todos ---")
    print(todos.render())


if __name__ == "__main__":
    asyncio.run(main())
