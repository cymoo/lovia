"""Multi-turn coding with a shared :class:`Workspace`.

Run::

    OPENAI_API_KEY=sk-... python examples/23_workspace_agent.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.stores import InMemorySession
from lovia.workspace import (
    Workspace,
    bash,
    edit_file,
    glob,
    list_dir,
    read_file,
    write_file,
)

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="lovia-demo-") as root:
        ws = Workspace(root=root)
        session = InMemorySession()
        agent = Agent(
            name="coder",
            instructions="Use the workspace tools to make small, verifiable edits.",
            model=MODEL,
            tools=[
                bash(ws),
                read_file(ws),
                write_file(ws),
                edit_file(ws),
                glob(ws),
                list_dir(ws),
            ],
        )

        first = await Runner.run(
            agent,
            "Create calc.py with an add(a, b) function and a quick self-test.",
            session=session,
            session_id="demo",
        )
        print(first.output)

        second = await Runner.run(
            agent,
            "Extend calc.py with subtract(a, b), then show the file.",
            session=session,
            session_id="demo",
        )
        print(second.output)


if __name__ == "__main__":
    asyncio.run(main())
