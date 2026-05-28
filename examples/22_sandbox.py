"""Sandbox basics: per-run :class:`LocalSandbox` + :func:`sandbox_tools`.

The simplest sandbox shape: build one ``LocalSandbox``, attach its tools
to an agent, run. Use this when the lifecycle is "one agent run, one
workspace" — e.g. ad-hoc scripts or notebook experiments. For multi-turn
sessions with sandbox reuse, see ``23_sandbox_session.py``.

Run::

    OPENAI_API_KEY=sk-... python examples/22_sandbox.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from dotenv import load_dotenv

from lovia import (
    Agent,
    LocalSandbox,
    Runner,
    default_audit_policy,
    sandbox_tools,
)

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="lovia-demo-") as root:
        async with LocalSandbox(root=root, ephemeral=False) as sb:
            # Seed a starter file the agent can read.
            await sb.write("README.md", "# scratchpad\n")

            agent = Agent(
                name="coder",
                instructions=(
                    "You have read_file/write_file/run/apply_patch tools "
                    "operating in a sandbox at /workspace. Keep work small. "
                    "If you need Python deps, bootstrap a venv first: "
                    "`python -m venv .venv && .venv/bin/pip install <pkg>`."
                ),
                model=MODEL,
                tools=sandbox_tools(sb, audit=default_audit_policy()),
            )

            result = await Runner.run(
                agent,
                "Write a Python script `hello.py` that prints 'hi from lovia' "
                "and run it. Report the output.",
            )
            print("=== model output ===")
            print(result.output)
            print("=== sandbox files ===")
            for entry in await sb.ls("."):
                print(f"  {entry.name} ({entry.size}B)")


if __name__ == "__main__":
    asyncio.run(main())
