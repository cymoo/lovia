"""Sandboxed filesystem — every path resolves under ``root``."""

from __future__ import annotations
import os

import asyncio
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.builtins.fs import FileSystem

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


async def main() -> None:
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "hello.txt").write_text("Hello from disk!\n")
        fs = FileSystem(root=root, writable=True)

        agent = Agent(
            name="Files",
            instructions="Use the filesystem tools to inspect and edit files.",
            model=MODEL,
            tools=fs.tools(),
        )
        result = await Runner.run(
            agent, "Read hello.txt, then append the line 'and disk says hi back.'"
        )
        print(result.output)
        print("---\nFinal content:")
        print((Path(root) / "hello.txt").read_text())


if __name__ == "__main__":
    asyncio.run(main())
