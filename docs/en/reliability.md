# Reliability

Agent runs fail in two distinct ways: infrastructure hiccups (a 429, a
dropped stream) and runaway behavior (a tool-call loop, a budget blowout).
lovia separates the knobs accordingly, with one placement rule:

- **Posture** — how the agent behaves when infrastructure misbehaves —
  lives on the `Agent` and every run inherits it: `retry`,
  `default_tool_retries` / `default_tool_timeout`, `context_policy`.
- **Limits** — how much one request may spend — are `Runner.run` arguments
  with no agent-side counterpart: `max_turns`, `budget`, `cancel_token`.

```python
from lovia import Agent, RetryPolicy, RunBudget, Runner, model_from_env

agent = Agent(name="analyst", model=model_from_env(),
              retry=RetryPolicy(max_attempts=2))          # posture

result = await Runner.run(
    agent,
    "Analyze these logs.",
    budget=RunBudget(max_tool_calls=20, max_seconds=60),  # limits
)
```

Per-call posture overrides exist (`Runner.run(..., retry=...,
context_policy=...)`) for the one request that really is special. The
*initial* agent's posture governs the whole run, handoffs included.

## Provider retries

Retries are **on by default**: `Agent.retry` defaults to `RetryPolicy()` —
5 total attempts (4 retries) with a jittered exponential backoff of roughly
1s / 2s / 4s / 8s, capped at 30s per wait. `retry=None` disables provider
retries entirely; `RetryPolicy(max_attempts=1)` is the per-run spelling of
the same thing.

| `RetryPolicy` field | Default | Meaning |
| --- | --- | --- |
| `max_attempts` | `5` | total calls per provider (first counts as 1) |
| `restart_on_partial` | `True` | recover from mid-stream failures by discarding partial output and re-streaming the turn |
| `backoff_base` / `backoff_max` | `1.0` / `30.0` | exponential schedule, ±50% jitter |
| `retry_on` | retryable `ProviderError`s | predicate deciding what counts as transient |

What counts as transient comes from the
[provider adapters](providers.md#networking-timeouts-proxies-tls): HTTP
408/429/5xx, network timeouts, and mid-stream disconnects are retryable;
4xx misconfiguration is not; `ContextOverflowError` never retries — it goes
to [reactive compaction](context.md) instead, which fixes the actual
problem.

**`restart_on_partial`** is the flag to know about: a provider dying
mid-stream after emitting half a paragraph is routine on long runs. With it
on (default), the runner discards the partial turn — emitting
[`OutputDiscarded`](streaming.md#model-output) so UIs clear what they
rendered — and re-streams from scratch; the transcript is assembled only
from completed turns, so nothing corrupts. With it off, mid-stream errors
propagate immediately.

**Vendor-level failover** is deliberately not an agent-loop feature: point
`base_url` at a routing gateway (LiteLLM, OpenRouter, ...) and let it fail
over server-side, or re-run the failed request against the same session
with a different model.

Tool-level retries are separate and off by default — per-tool
`@tool(retries=..., timeout=...)` or agent-wide `default_tool_retries` /
`default_tool_timeout` ([Tools](tools.md#retries-and-timeouts)).

## Budgets

`RunBudget` puts hard caps on one run. The runner checks it between turns,
after each model reply, and at every tool call's preflight:

| Field | Caps |
| --- | --- |
| `max_input_tokens` / `max_output_tokens` / `max_total_tokens` | cumulative tokens |
| `max_tool_calls` | *requested* tool calls — rejected ones included, so a model spamming a bad tool name still hits the cap |
| `max_seconds` | wall clock, from the first check |

Semantics: tripping raises `BudgetExceeded` at the next safe point —
in-flight tool calls are allowed to **finish and persist** (a trip stops
*dispatching*, not work already running). A budget instance carries
single-run state (its clock, its call count): **create a fresh one per
run**. Inside an [agent-as-tool](multi-agent.md#agent-as-tool) sub-run, the
sub-run's own exhausted budget becomes a tool-error result the parent can
react to, not a run-ending failure.

`max_turns` (default 50) is the simplest limit of all: exceeding it raises
`MaxTurnsExceeded`.

## Cancellation

Cooperative, via a token the runner checks between turns, at each
preflight, and after each completed tool result:

```python
from lovia import CancelToken, Runner

token = CancelToken()
handle = Runner.stream(agent, "Long analysis...", cancel_token=token)
# from anywhere:
token.cancel("user clicked stop")        # or: handle.cancel("...")
```

The run ends with `RunCancelled` (stream: `RunFailed`) at the next safe
point; a mid-batch cancel also cancels the batch's still-running sibling
calls. The token is always present on the run — tools and hooks reach it as
`ctx.cancel_token`, so a run can cancel *itself* (a hook that spots a
poison pattern, a tool that detects an unrecoverable state). Sub-runs
inherit the parent's token: one cancel stops the whole tree.

Two things cancellation cannot do: interrupt a **sync** tool's worker
thread (the thread finishes; its effects may land after the run ends), and
un-send a request already at the provider.

## Steering a live run

The inbound dual of cancellation: a `Mailbox` carries messages *into* a
running agent. The runner drains it at the start of every turn and appends
each item as a normal user message:

```python
from lovia import Mailbox, Runner

mailbox = Mailbox()
handle = Runner.stream(agent, "Analyze these logs.", mailbox=mailbox)
mailbox.push("Focus on the 5xx spike around 14:00.")   # seen next turn
```

Tools and hooks reach the same channel as `ctx.mailbox` — the runner
creates one per run when you don't supply it — so a run can steer itself
with no outside plumbing:

```python
from lovia import RunContext, events
from lovia.hooks import AgentHooks

hooks = AgentHooks()

@hooks.on(events.TurnStarted)
def deadline(ev, ctx: RunContext):
    if ev.turn == 9:
        ctx.mailbox.push("Last turn: answer with what you have.")
```

The semantics, precisely:

- Drains happen at **turn starts** only, never mid-turn. A `TurnStarted`
  hook fires just *before* its turn's drain, so a push from that hook lands
  on that very turn; pushes from anywhere else land on the following turn.
- Each drained message emits
  [`UserMessageInjected`](streaming.md#model-output) and is persisted
  immediately (a crash can't drop consumed messages).
- `push()` returns a token; `remove(token)` withdraws a message that
  hasn't been drained yet.
- Leftovers: whatever is queued when the run ends stays in a
  **caller-supplied** mailbox (feed it to the next run); a runner-created
  default is unreachable after the run — a push during the final turn goes
  to nobody.
- [Agent-as-tool](multi-agent.md#agent-as-tool) sub-runs get their own
  mailbox, deliberately not the parent's.

## Sharp edges

- **Retries multiply latency before they surface errors.** Five attempts
  with backoff can hold a turn for ~15s before failing; interactive UIs
  usually want `max_attempts=2` posture and let the user retry.
- **`max_seconds` is not a deadline.** It trips at the next *check* — a
  60s budget with a 5-minute tool call ends at ~5 minutes. Enforce true
  deadlines with per-tool `timeout=` plus a cancel token from your own
  timer.
- **Budgets don't span retries of your own.** Re-running a failed request
  with the same `RunBudget` instance carries the spent clock and counters —
  build a fresh budget (this is why agent-as-tool copies per invocation).
- **A steered message is a *user* message.** The model weighs it like any
  user turn — it does not preempt tool calls already requested, and it
  persists in the session like everything else.

## See also

- [Providers & models](providers.md) — what's retryable, fallback chains
- [Sessions & checkpoints](sessions-and-checkpoints.md) — recovery *across*
  process crashes (retries are recovery *within* a run)
- Examples: [`14_reliability.py`](../../examples/14_reliability.py),
  [`16_steering.py`](../../examples/16_steering.py)
