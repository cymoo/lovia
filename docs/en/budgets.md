# Budgets & limits

Retries describe how a run reacts to infrastructure. Limits describe how much
one request may spend. Pass them to the Runner because they belong to a single
run, not to reusable Agent configuration.

```python
from lovia import Agent, RunBudget, Runner

agent = Agent(name="analyst", model="<model>")

result = await Runner.run(
    agent,
    "Analyze these logs.",
    max_turns=12,
    budget=RunBudget(max_tool_calls=20, max_seconds=60),
)
```

## RunBudget

The runner checks the budget between turns, after every model reply, and at
each tool call's preflight.

| Field | Limit |
| --- | --- |
| `max_input_tokens` | Cumulative input tokens |
| `max_output_tokens` | Cumulative output tokens |
| `max_total_tokens` | Cumulative input plus output tokens |
| `max_tool_calls` | Requested tool calls, including rejected calls |
| `max_seconds` | Wall-clock time from the first budget check |

Crossing a limit raises `BudgetExceeded` at the next safe point. A limit stops
new tool calls from being dispatched, but calls already running are allowed to
finish and persist their results.

!!! warning "Create a fresh budget per run"

    A `RunBudget` stores its start time and tool-call count. Reusing the same
    instance carries spent time and calls into the next run.

An [Agent-as-tool](multi-agent.md#agent-as-tool) sub-run uses its own copied
budget. If that sub-run exceeds the limit, the parent sees a tool error and can
adapt instead of failing automatically.

## Turn limit

`max_turns` defaults to `50` and raises `MaxTurnsExceeded` when exhausted. It
is the clearest guard against an Agent repeatedly calling tools without
reaching a final answer.

## Time limits are cooperative

`max_seconds` is checked at safe points; it is not a hard deadline. A tool that
runs for five minutes can make a 60-second budget finish late. Combine it with
per-tool `timeout=` and [cancellation](cancellation.md) when real deadlines
matter.

## See also

- [Provider retries](retries.md) — recover from transient model failures
- [Tools](tools.md#retries-and-timeouts) — per-attempt timeouts
- [Cancellation & steering](cancellation.md) — stop a live run
