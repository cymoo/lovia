"""Production reliability: budgets, retries, timeouts, cancellation, fallback.

Five orthogonal safety nets, each one line to adopt:

* :class:`RunBudget` caps tokens, tool calls, and wall-clock per run.
* :class:`RetryPolicy` retries transient provider errors with backoff.
* ``@tool(retries=..., timeout=...)`` does the same for a flaky tool.
* :class:`CancelToken` cooperatively cancels a run from outside.
* ``model=[primary, fallback]`` fails over to the next provider when the
  first keeps erroring.

Run::

    python examples/14_reliability.py
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
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )

# Optional second model demonstrating the provider fallback chain.
FALLBACK_MODEL = os.environ.get("LOVIA_FALLBACK_MODEL")

_attempts = {"count": 0}


@tool(retries=2, timeout=5.0)
async def stock_quote(symbol: str) -> str:
    """Return a quote from a backend that fails on its first attempt."""
    _attempts["count"] += 1
    if _attempts["count"] == 1:
        raise RuntimeError("upstream hiccup")  # retried transparently
    return f"{symbol}: 101.70 (attempt {_attempts['count']})"


async def main() -> None:
    agent = Agent(
        name="resilient",
        instructions="Answer concisely using the stock_quote tool.",
        # ``model`` accepts a list; the runner falls through on repeated
        # provider errors. Set LOVIA_FALLBACK_MODEL to arm the chain — without
        # it the agent runs on the primary model alone.
        model=[MODEL, FALLBACK_MODEL] if FALLBACK_MODEL else MODEL,
        tools=[stock_quote],
    )

    budget = RunBudget(
        max_output_tokens=2_000,
        max_tool_calls=10,
        max_seconds=60,
    )
    retry = RetryPolicy(max_attempts=3)  # provider-level, with backoff
    cancel = CancelToken()

    # Cancel the run from outside if it takes too long overall.
    async def watchdog() -> None:
        await asyncio.sleep(30)
        cancel.cancel()

    watchdog_task = asyncio.create_task(watchdog())

    try:
        result = await Runner.run(
            agent,
            "Get the ACME quote and comment on it in one sentence.",
            budget=budget,
            retry=retry,
            cancel_token=cancel,
        )
        print(result.output)
        print("usage:", result.usage)
    except BudgetExceeded as exc:
        print("budget hit:", exc)
    finally:
        watchdog_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
