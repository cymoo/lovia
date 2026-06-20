"""Skills — reusable instruction bundles with progressive disclosure.

This example uses the ``examples/skills/refund-policy`` directory, which
contains a realistic skill with:

* ``SKILL.md`` — YAML frontmatter + markdown instructions (Level 2)
* ``references/international-orders.md`` — supplementary docs (Level 3)
* ``scripts/calculate_refund.py`` — executable script
* ``assets/refund-email.txt`` — email template
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from lovia import Agent, Runner, Skills, AgentHooks, RunContext
from lovia.events import (
    TextDelta,
    ReasoningDelta,
    ToolCallStarted,
    ToolCallCompleted,
    RunStarted,
    RunCompleted,
)
from lovia.workspace import Workspace

load_dotenv()

SKILLS_DIR = Path(__file__).parent / "skills"

async def main() -> None:
    # Provide a shell tool so the model can execute skill scripts.
    agent = Agent(
        name="SupportBot",
        instructions= "You are a customer support agent.",
        model=os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4"),
        workspace=Workspace.local('.', mode="trusted"),
        plugins=[Skills(SKILLS_DIR)],
    )

    print("=" * 60)
    print("SupportBot — Skills Demo")
    print("=" * 60)

    handle = Runner.stream(
        agent,
        "A customer from Germany bought a laptop 37 days ago for $1299 and "
        "wants a refund. Use the calculate_refund script to compute the exact "
        "refund amount, then check the international orders reference for any "
        "EU-specific rules.",
    )

    in_reasoning = False

    async for event in handle:
        if isinstance(event, RunStarted):
            print(f"\n🚀 Agent: {event.agent.name}")

        elif isinstance(event, ReasoningDelta):
            if not in_reasoning:
                print("\n💭 ", end="", flush=True)
                in_reasoning = True
            print(event.delta, end="", flush=True)

        elif isinstance(event, TextDelta):
            in_reasoning = False
            print(event.delta, end="", flush=True)

        elif isinstance(event, ToolCallStarted):
            in_reasoning = False
            print(f"\n   🔧 {event.call.name}({event.call.arguments})", flush=True)

        elif isinstance(event, ToolCallCompleted):
            name = event.call.name
            rv = event.result
            if isinstance(rv, str) and len(rv) > 150:
                rv = rv[:150] + "…"
            status = "❌" if event.is_error else "✔"
            print(f"   {status} {name} → {rv!r}", flush=True)

        elif isinstance(event, RunCompleted):
            r = event.result
            print(f"\n{'=' * 60}")
            print(
                f"Done in {r.turns} turns.  "
                f"in={r.usage.input_tokens}  out={r.usage.output_tokens}"
            )

    final = await handle.result()
    print(f"Output: {final.output}")


if __name__ == "__main__":
    asyncio.run(main())
