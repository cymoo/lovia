"""Workspace basics: local code tools backed by :class:`Workspace`.

Run::

    OPENAI_API_KEY=sk-... python examples/22_workspace.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from dotenv import load_dotenv

from lovia import Agent, Runner
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
        await ws.write_file("README.md", "# scratchpad\n")

        agent = Agent(
            name="coder",
            instructions=(
                "You have bash/read_file/write_file/edit_file/glob/list_dir tools "
                "operating in a local workspace at /workspace. Keep work small."
            ),
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

        result = await Runner.run(
            agent,
            "Write a Python script `hello.py` that prints 'hi from lovia' "
            "and run it. Report the output.",
        )
        print("=== model output ===")
        print(result.output)
        print("=== workspace files ===")
        for entry in await ws.list_dir("."):
            print(f"  {entry.name} ({entry.size}B)")


if __name__ == "__main__":
    asyncio.run(main())
