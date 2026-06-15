"""Production safety nets: budgets, retries, cancellation, provider fallback.

This example demonstrates four orthogonal reliability primitives:

* :class:`RunBudget` caps tokens, tool calls and wall-clock per run.
* :class:`RetryPolicy` retries transient provider errors with backoff.
* :class:`CancelToken` cooperatively cancels a run from outside.
* A ``model=[...]`` list creates an automatic provider fallback chain — if
  the first provider keeps failing, the next one is tried.
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import (
    Agent,
    BudgetExceeded,
    CancelToken,
    RetryPolicy,
    RunBudget,
    Runner,
    tool,
)

load_dotenv()


@tool
async def slow_search(query: str) -> str:
    """Pretend to do an expensive search."""
    await asyncio.sleep(0.1)
    return f"results for {query!r}: 42"


async def main() -> None:
    agent = Agent(
        name="resilient",
        instructions="Answer concisely.",
        # ``model`` accepts a list of providers; the runner falls through on
        # repeated provider errors. Here we just use one DeepSeek model, but
        # you could add a second model name as a backup.
        model=[
            os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4"),
            os.getenv("OPENAI_FALLBACK_MODEL", "deepseek-chat"),
        ],
        tools=[slow_search],
    )

    budget = RunBudget(
        max_output_tokens=2_000,
        max_tool_calls=10,
        max_seconds=60,
    )
    retry = RetryPolicy(max_attempts=3)
    cancel = CancelToken()

    # Cancel the run after 5 seconds from another task.
    async def watchdog() -> None:
        await asyncio.sleep(5)
        cancel.cancel()

    asyncio.create_task(watchdog())

    try:
        result = await Runner.run(
            agent,
            "Search for 'lovia' and summarise.",
            budget=budget,
            retry=retry,
            cancel_token=cancel,
        )
        print(result.output)
        print("usage:", result.usage)
    except BudgetExceeded as exc:
        print("budget hit:", exc)


if __name__ == "__main__":
    asyncio.run(main())
