# Running agents

The three `Runner` entry points execute the same RunLoop and differ only in
how the result is consumed:

| Environment | Entry point |
| --- | --- |
| Async application, final result only | `await Runner.run(...)` |
| Regular script or REPL | `Runner.run_sync(...)` |
| UI, CLI, or intermediate events | `Runner.stream(...)` |

`agent.run(...)` / `agent.run_sync(...)` / `agent.stream(...)` are the same
calls as instance methods.

## How the three entry points behave

**`Runner.run(agent, input, **options) -> RunResult`** — awaits the run and
returns the final result. Failures raise (`GuardrailTripped`,
`BudgetExceeded`, `ProviderError`, ... — see [When things go wrong](concepts.md#when-things-go-wrong)).

**`Runner.run_sync(...)`** — the same, wrapped in `asyncio.run()` for code
that isn't async yet. Calling it from inside a running event loop raises
`UserError` with the fix in the hint (use `await Runner.run(...)`).

**`Runner.stream(...) -> RunHandle`** — starts the run and hands back a
handle that is both **async-iterable** (typed events) and **awaitable** (the
final result):

```python
handle = Runner.stream(agent, "Analyze these logs.")

async for ev in handle:          # never raises for run failures
    ...

result = await handle.result()   # returns the RunResult, or raises the run's error
```

Iteration is single-shot (a second `async for` raises `RuntimeError`) and
always ends with exactly one terminal event — `RunCompleted` or `RunFailed`.
`await handle` is shorthand for `await handle.result()`; if nothing has
iterated the stream yet, `result()` drives it to completion itself.
`handle.cancel()` requests cooperative cancellation without pre-wiring a
`CancelToken`, and `handle.approvals` is the out-of-band
[approval channel](tools.md#tool-approval).
The events themselves are catalogued in [Streaming](streaming.md).

## Run options

All three entry points accept the same keywords:

| Option | Default | What it does |
| --- | --- | --- |
| `context` | `None` | your dependency object, surfaced as `ctx.deps` ([Agents](agents.md#inject-application-dependencies)) |
| `output_type` | `None` | run-wide override of the agent's [output type](structured-output.md) |
| `extra_instructions` | `None` | per-run system-prompt addendum, rendered after the agent's own instructions (and re-applied to every agent a handoff reaches) |
| `max_turns` | `50` | hard cap on model turns; exceeding it raises `MaxTurnsExceeded` |
| `budget` | `None` | a `RunBudget` limiting what the run may spend ([Budgets](budgets.md)) |
| `cancel_token` | `None` | pre-wired cooperative cancellation ([Cancellation](cancellation.md#cancellation)) |
| `mailbox` | `None` | inbound steering channel ([Steering](cancellation.md#steering-a-live-run)) |
| `retry` | agent's | per-call override of the provider retry posture |
| `context_policy` | agent's | per-call override of the [context policy](context.md) |
| `session` + `session_id` | `None` | conversation persistence ([Sessions & checkpoints](sessions-and-checkpoints.md)) |
| `checkpoint` | `None` | crash recovery and idempotent runs ([Sessions & checkpoints](sessions-and-checkpoints.md#checkpoints)) |
| `tracer` | `None` | run-scoped tracing ([Observability](observability.md#tracing)) |

`retry` and `context_policy` are the two *posture* overrides — they default
to the agent's own configuration, and the **initial** agent's posture
governs the whole run even across handoffs. The rest are *limits and
wiring*, which deliberately have no agent-side counterpart.

## Inputs

`input` is either a string (one user message) or a list of `Message` values
for multi-message openings:

```python
from lovia import Runner, system, user

result = await Runner.run(
    agent,
    [
        system("Answer as a pirate."),   # extra system message, kept in the transcript
        user("Where do we sail?"),
    ],
)
```

### Images and files

Message content can be a list of typed parts instead of a string —
`TextPart`, `ImagePart`, `FilePart` — and providers translate them to their
wire format:

```python
from lovia import ImagePart, Runner, TextPart, user

result = await Runner.run(
    agent,
    [
        user(
            [
                TextPart("What's in this screenshot?"),
                ImagePart.from_path("shot.png"),
            ]
        )
    ],
)
```

(`user(...)` also accepts a plain string, or a single part; a part *list*
must contain typed parts — a bare `str` inside the list is not coerced.)

- `ImagePart(url=...)` or `ImagePart(data=..., mime_type=...)` — exactly one
  of `url`/`data`, and base64 `data` requires `mime_type`.
  `ImagePart.from_path()` loads and encodes a local file, inferring the mime
  type from the suffix. Optional `detail="low"|"high"|"auto"`.
- `FilePart` — same shape plus `filename`; constructors `from_path`,
  `from_bytes`, `from_base64`, `from_url`. URL parts are provider-native
  references — lovia never downloads them.

## The result

| `RunResult` field | What it is |
| --- | --- |
| `output` | the final answer — `str`, or a validated instance of the run's `output_type` |
| `entries` | **this run's own** transcript: its input plus everything it produced, across handoffs |
| `messages` | chat-format view derived from `entries` (lossy) |
| `final_agent` | the agent that produced the output (differs from the initial one after a handoff) |
| `usage` | cumulative tokens — `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `total_tokens`; agent-as-tool sub-runs included |
| `turns` | how many model turns the run took |
| `finish_reason` | the final turn's provider-reported reason — check `"stop"` vs `"length"` to detect a `max_tokens`-truncated answer |

`entries` deliberately excludes the system prompt and prior session history,
so it is identical whether the run finished fresh or was rebuilt from a
checkpoint. For the full conversation, read `ctx.entries` inside a hook, or
`session.load()` after the run.

## Sharp edges

- **`RunResult.entries` is not the whole transcript** — it's the run's
  delta. Code that renders "the conversation" from it will silently drop
  prior history; use the session for that.
- **`finish_reason` can be `None`** — when the provider reported none, or
  when the result was replayed from a completed checkpoint (it isn't
  persisted in snapshots).
- **A model reply with no content and no tool calls completes the run** with
  an empty-string output (logged as a warning). It's almost always a
  provider hiccup or `max_tokens` truncation — check `finish_reason` before
  trusting an empty answer.
- **`run_sync` owns the event loop** — it refuses to run inside one. In
  notebooks with a live loop, use `await Runner.run(...)`.

## See also

- [Streaming](streaming.md) — the event catalog behind `Runner.stream`
- [Sessions & checkpoints](sessions-and-checkpoints.md) — persistence options
- [Production controls](budgets.md) — budgets, cancellation, steering, and retries
- Examples: [`01_hello.py`](../../examples/01_hello.py),
  [`06_multimodal.py`](../../examples/06_multimodal.py)
