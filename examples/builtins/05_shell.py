"""Shell — dangerous by default; opt in with ``needs_approval=allowlist(...)``.

Without an approval predicate every command would raise ``ApprovalRequired``.
"""

from __future__ import annotations

import asyncio
import tempfile

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.builtins.shell import Shell, allowlist

load_dotenv()


async def main() -> None:
    with tempfile.TemporaryDirectory() as cwd:
        sh = Shell(cwd=cwd, needs_approval=allowlist(["ls", "echo", "pwd"]))
        agent = Agent(
            name="Shell",
            instructions="Use the shell to inspect the current directory.",
            model="openai:gpt-4o-mini",
            tools=[sh.tool()],
        )
        result = await Runner.run(agent, "Print pwd and list files.")
        print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
