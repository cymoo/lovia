"""A miniature agent that combines several builtins in one workflow."""

from __future__ import annotations

import asyncio
import tempfile

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.builtins.fs import FileSystem
from lovia.builtins.http import http_fetch
from lovia.builtins.think import think
from lovia.builtins.time import now
from lovia.builtins.todo import TodoList, todo_tools

load_dotenv()


async def main() -> None:
    with tempfile.TemporaryDirectory() as root:
        fs = FileSystem(root=root, writable=True)
        todos = TodoList()

        agent = Agent(
            name="Mini",
            instructions=(
                "Plan with todos, think out loud, then act. "
                "You can read time, fetch URLs, and write files under the sandbox."
            ),
            model="openai:gpt-4o-mini",
            tools=[
                http_fetch,
                now,
                think,
                *fs.tools(),
                *todo_tools(todos),
            ],
        )
        result = await Runner.run(
            agent,
            "Fetch https://httpbin.org/uuid and save the JSON to today.json.",
        )
        print(result.output)
        print("\n--- Todos ---")
        print(todos.render())


if __name__ == "__main__":
    asyncio.run(main())
