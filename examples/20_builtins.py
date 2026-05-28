"""Opt-in builtin tools from ``lovia.builtins.*``.

Nothing in ``lovia.builtins`` is imported by ``import lovia`` — pick
exactly what you need. Stateful helpers expose ``.tool()`` / ``.tools()``;
stateless helpers are module-level ``Tool`` instances.

Run::

    OPENAI_API_KEY=sk-... python examples/20_builtins.py
"""

from __future__ import annotations

import asyncio
import tempfile

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.builtins.fs import FileSystem
from lovia.builtins.http import http_fetch
from lovia.builtins.shell import Shell, allowlist
from lovia.builtins.think import think
from lovia.builtins.time import now
from lovia.builtins.todo import TodoList, todo_tools

load_dotenv()


async def main() -> None:
    with tempfile.TemporaryDirectory() as work:
        fs = FileSystem(root=work, writable=True)
        sh = Shell(cwd=work, needs_approval=allowlist(["ls", "echo", "cat"]))
        todos = TodoList()

        agent = Agent(
            name="Worker",
            instructions=(
                "You have shell, filesystem, http, time, think, and todo tools. "
                "Plan steps with todos. Use 'think' to reason out loud."
            ),
            model="openai:gpt-4o-mini",
            tools=[
                http_fetch,
                now,
                think,
                *fs.tools(),
                sh.tool(),
                *todo_tools(todos),
            ],
        )

        result = await Runner.run(
            agent,
            "Write 'hello' to greeting.txt, then read it back and tell me what you saw.",
        )
        print(result.output)
        print("\nFinal todos:")
        print(todos.render())


if __name__ == "__main__":
    asyncio.run(main())
