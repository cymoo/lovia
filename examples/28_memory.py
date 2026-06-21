"""Long-term memory that persists across runs with the ``Memory`` plugin.

``Memory`` gives the agent two tiers it curates with verbs it already knows:

* **Notes** (hot) — a small block always injected into the system prompt; the
  model writes to it with ``remember(fact)`` / ``forget(fact)`` and the plugin
  promotes durable facts there automatically at run end.
* **Archive** (cold) — a full-text-searchable log of past conversations the
  model pulls in on demand with ``recall(query)``.

Both live under the directory you pass, so a fact learned in one run is known
in the next — even in a fresh process. Delete ``/tmp/lovia_memory`` to reset.

Run::

    python examples/28_memory.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, Memory, Runner

load_dotenv()

MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4")


async def main() -> None:
    agent = Agent(
        name="assistant",
        instructions="You are a concise personal assistant.",
        model=MODEL,
        plugins=[Memory("/tmp/lovia_memory")],
    )

    # First run: state a durable preference. The plugin promotes it into Notes
    # at run end (one model call) without you wiring anything up.
    r1 = await Runner.run(agent, "Remember that I'm vegetarian and I live in Kyoto.")
    print("A:", r1.output)

    # Second run — a brand-new run with no shared transcript. The fact survives
    # because it was written to Notes, which is injected into every run's prompt.
    r2 = await Runner.run(agent, "Suggest a dinner. Keep my preferences in mind.")
    print("A:", r2.output)


if __name__ == "__main__":
    asyncio.run(main())
