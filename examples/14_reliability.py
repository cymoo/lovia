"""Production reliability: budgets, retries, timeouts, cancellation.

Four orthogonal safety nets, each one line to adopt. Note the placement
rule: *posture* (provider retries, per-tool retries/timeouts) is Agent
config; *limits* (budget, wall-clock, cancellation) are per-run arguments.

* ``Agent(retry=RetryPolicy(...))`` retries transient provider errors with
  backoff — for every run of this agent. A run can still override it.
* ``@tool(retries=..., timeout=...)`` does the same for a flaky tool.
* :class:`RunBudget` caps tokens, tool calls, and wall-clock per run.
* :class:`CancelToken` cooperatively cancels a run from outside. (When you
  hold the stream handle, ``handle.cancel()`` does this without wiring —
  see 15_resume.py.)

Vendor-level failover is deliberately not in this list: point ``base_url``
at a routing gateway (LiteLLM, OpenRouter, ...) that fails over server-side,
or re-run a failed request against the same session with another model.

Run::

    python examples/14_reliability.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import (
    Agent,
    BudgetExceeded,
    CancelToken,
    RetryPolicy,
    RunBudget,
    RunCancelled,
    Runner,
    model_from_env,
    tool,
)

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

_attempts = {"count": 0}


@tool(retries=2, timeout=5.0)
async def stock_quote(symbol: str) -> str:
    """Return a quote from a backend that fails on its first attempt."""
    _attempts["count"] += 1
    if _attempts["count"] == 1:
        raise RuntimeError("upstream hiccup")  # retried transparently
    return f"{symbol}: 101.70 (attempt {_attempts['count']})"


agent = Agent(
    name="resilient",
    instructions="Answer concisely using the stock_quote tool.",
    model=MODEL,
    tools=[stock_quote],
    # Provider-retry posture rides on the agent (default: RetryPolicy()).
    retry=RetryPolicy(max_attempts=3),
)


async def main() -> None:
    budget = RunBudget(
        max_output_tokens=2_000,
        max_tool_calls=10,
        max_seconds=60,
    )
    cancel = CancelToken()

    # Cancel the run from outside if it takes too long overall.
    async def watchdog() -> None:
        await asyncio.sleep(30)
        cancel.cancel("watchdog timeout")

    watchdog_task = asyncio.create_task(watchdog())

    try:
        result = await Runner.run(
            agent,
            "Get the ACME quote and comment on it in one sentence.",
            budget=budget,
            cancel_token=cancel,
        )
        print(result.output)
        print("usage:", result.usage)
    except BudgetExceeded as exc:
        print("budget hit:", exc)
    except RunCancelled as exc:
        print("cancelled:", exc)
    finally:
        watchdog_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
