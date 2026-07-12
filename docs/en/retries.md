# Provider retries

Transient provider failures are normal: rate limits, timeouts, and interrupted
streams should not require application-specific retry loops. lovia applies a
retry policy around each model turn.

```python
from lovia import Agent, RetryPolicy, Runner

agent = Agent(
    name="analyst",
    model="<model>",
    retry=RetryPolicy(max_attempts=2),
)

result = await Runner.run(agent, "Analyze these logs.")
```

`Agent.retry` is posture: every run inherits it. Pass `retry=` to a Runner
entry point only when one request needs a different policy. The initial
Agent's policy governs the whole run, including handoffs.

## Retry policy

Retries are on by default. `Agent.retry` defaults to `RetryPolicy()`: five
total attempts with jittered exponential backoff. Use
`RetryPolicy(max_attempts=1)` to disable retries for one run, or `retry=None`
on the Agent to disable them by default.

| `RetryPolicy` field | Default | Description |
| --- | --- | --- |
| `max_attempts` | `5` | Total provider calls; the first attempt counts |
| `restart_on_partial` | `True` | Discard incomplete streamed text and restart the turn |
| `backoff_base` | `1.0` | Initial backoff in seconds |
| `backoff_max` | `30.0` | Maximum delay between attempts |
| `retry_on` | retryable `ProviderError`s | Predicate that decides whether an error is transient |

Provider adapters mark HTTP 408, 429, 5xx, network timeouts, and interrupted
streams as retryable. Configuration errors generally are not. A
`ContextOverflowError` goes through [context compaction](context.md), not the
retry policy.

## Partial streams

With `restart_on_partial=True`, the runner emits
[`OutputDiscarded`](streaming.md#model-output), discards the unfinished model
turn, and starts it again. The canonical transcript only receives completed
turns, so partial text never becomes history. UIs should clear already rendered
text when they receive this event.

Set the option to `False` when duplicating a streamed response is worse than
surfacing the failure immediately.

## Tool retries are separate

Provider retries repeat a model request. Tool retries repeat one tool attempt
and are off by default. Configure them per tool with
`@tool(retries=..., timeout=...)` or on the Agent with
`default_tool_retries` and `default_tool_timeout`; see
[Tools](tools.md#retries-and-timeouts).

!!! tip "Keep interactive retries short"

    Five attempts can add noticeable latency. Interactive applications often
    use `RetryPolicy(max_attempts=2)` and let the user explicitly retry after
    the error is visible.

## See also

- [Providers & models](providers.md) — networking and provider behavior
- [Budgets & limits](budgets.md) — cap the cost of a run
- [Sessions & checkpoints](sessions-and-checkpoints.md) — recover across process crashes
- Example: [`14_reliability.py`](../../examples/14_reliability.py)
