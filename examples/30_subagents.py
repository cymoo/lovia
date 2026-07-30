"""Spawn background subagents that work while the parent keeps going.

Unlike ``as_tool`` (which waits for the child), the ``Subagents`` plugin's
``spawn_subagent`` returns immediately: children run concurrently on the
event loop, their reports arrive as messages at turn boundaries, and
``wait_subagents`` collects whatever is still pending before the final
answer. Try a prompt that splits into independent lookups.

Run::

    python examples/30_subagents.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, Subagents, model_from_env
from lovia.tools import duckduckgo_search

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

researcher = Agent(
    name="researcher",
    instructions=(
        "Research the topic you are given and reply with a compact, factual "
        "report: 3-5 bullet points, one line each."
    ),
    model=MODEL,
    tools=[duckduckgo_search()],  # pip install lovia[ddg]
)

assistant = Agent(
    name="assistant",
    instructions=(
        "For questions that split into independent parts, delegate each part "
        "to a background researcher, keep reasoning about structure while "
        "they run, then weave the reports into one answer."
    ),
    model=MODEL,
    plugins=[Subagents([researcher], max_concurrent=3)],
)


async def main() -> None:
    result = await Runner.run(
        assistant,
        "Compare the Python 3.13 free-threading build and PyPy: what each "
        "is, current status, and when to pick which.",
    )
    print(result.output)
    print(f"\n[usage: {result.usage.total_tokens} tokens, tree total]")


if __name__ == "__main__":
    asyncio.run(main())
