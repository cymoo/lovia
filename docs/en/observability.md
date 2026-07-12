# Observability

You can't fix what you can't see, and an agent's failure modes are
mid-run — the tool that took 40 seconds, the turn that burned 30k tokens.
Three instruments, smallest first: **hooks** (react to events), **tracing**
(timed spans), **logging** (the built-in narrative).

## Hooks

`AgentHooks` is a subscriber: attach handlers per event type, and the
runner dispatches every event through them — the *same* typed events
[streaming](streaming.md) yields, so instrumentation works even when nobody
consumes the stream.

```python
from lovia import Agent, RunContext, events
from lovia.hooks import AgentHooks

hooks = AgentHooks()


@hooks.on(events.ToolCallStarted)
async def log_tool(ev: events.ToolCallStarted, ctx: RunContext):
    print("→", ev.call.name, "in session", ctx.session_id)


@hooks.on((events.RunCompleted, events.RunFailed))   # a tuple registers both
def at_end(ev, ctx):
    metrics.count("runs", tags={"ok": isinstance(ev, events.RunCompleted)})


@hooks.on_any
def firehose(ev, ctx):
    audit_log.write(type(ev).__name__)


agent = Agent(..., hooks=hooks)
```

The contract:

- Every handler is called as `handler(event, ctx)` — the event plus the
  run's live [`RunContext`](concepts.md#runcontext-the-one-handle)
  (`session_id`, active agent, cumulative usage, transcript, cancel token,
  mailbox). Handlers may be sync or async.
- Registration is by concrete type with `isinstance` matching, so
  subscribing to a base class (`events.ToolEvent`) catches the family;
  multiple handlers per type run in registration order, catch-alls first.
- **Fail-open**: a raising handler is logged (warning, with traceback) and
  skipped — broken metrics must not abort the run they watch.
- Ordering guarantee: events reach hooks in emission order, at the loop's
  single dispatch point — the same order the stream consumer sees.
- Hooks aren't observers only: `ctx` is live, so a handler can push into
  `ctx.mailbox` ([steering](cancellation.md#steering-a-live-run)) or trip
  `ctx.cancel_token`.

[Plugins](plugins.md) can contribute their own `AgentHooks`, dispatched
alongside the agent's — how [Memory](memory.md) triggers its run-end
curation.

## Tracing

Hooks tell you *what happened*; spans tell you *what took how long, inside
what*. The `Tracer` protocol is one method — `span(name, **attributes)` as
a context manager — and the runner emits four span types when a tracer is
passed to a run:

| Span | Attributes |
| --- | --- |
| `run` | `agent`, `run_id` (+ `turns`, `total_tokens`, `resumed` at end) |
| `model_call` | `model`, `turn` |
| `tool_call` | `name`, `call_id` |
| `handoff` | `from_agent`, `to_agent` |

```python
from lovia import Runner
from lovia.tracing import ConsoleTracer

result = await Runner.run(agent, "...", tracer=ConsoleTracer(min_duration_ms=5))
```

Three implementations ship: `NoopTracer` (the default — instrumentation
stays free), `ConsoleTracer` (indented tree via `logging`, for local
debugging), and `InMemoryTracer` (records `RecordedSpan`s for test
assertions). For production, adapt your backend — OpenTelemetry, Logfire —
by implementing the two-method protocol (`span()` returning something with
`set_attribute` / `record_exception`).

The tracer is a **run-level** knob, not an agent field: it applies across
handoffs to whichever agent is active, and
[agent-as-tool](multi-agent.md#agent-as-tool) sub-runs inherit it so their
spans join the parent's trace.

## Logging

lovia logs a structured narrative under the `lovia` logger —
`run.start`, `model.done: turn=2 tokens=1841(in=1520 out=321) …`,
`tool.start`/`tool.error`, `run.handoff`, `context.overflow`,
`run.done` — with a `NullHandler` attached by default (a library must stay
silent until asked). For scripts and notebooks:

```python
from lovia import enable_logging

enable_logging()                      # INFO on stderr, color when a TTY
enable_logging("DEBUG", color=False)  # more detail, no ANSI
```

`enable_logging` is idempotent (re-calling replaces its own handler), honors
`NO_COLOR`, and doesn't propagate to the root logger by default (no double
printing under uvicorn — `propagate=True` opts back in). Production apps
should configure `logging` themselves and ignore this helper.

## Usage accounting

Every run accumulates a `Usage` — on `result.usage`, live on `ctx.usage`,
and folded upward from sub-runs:

| Field | Meaning |
| --- | --- |
| `input_tokens` | **full** prompt size, cached tokens included |
| `output_tokens` | completion tokens |
| `cache_read_tokens` / `cache_write_tokens` | the [prompt-cache](providers.md#prompt-caching) breakdown of the input |
| `total_tokens` | input + output |

The cache fields *subdivide* `input_tokens`, they don't add to it — cost
formulas are `(input - cache_read) * rate_in + cache_read * rate_cached + …`.
For per-turn deltas, hook `RunCompleted`/`TurnEnded` and diff, or read each
model turn's own usage from the `model.done` log line.

## Sharp edges

- **Hooks run inline on the loop.** A slow handler delays the run —
  dispatch is awaited between events. Ship metrics async (queue + worker),
  not with a blocking HTTP call per event.
- **Hook mutations are real.** `ctx.entries` is the live transcript; treat
  it read-only. The safe mutations are the designed ones — mailbox, cancel.
- **`ConsoleTracer` is for humans**, not for parsing — its format is
  unversioned. Structured needs → implement `Tracer` against your backend.
- **Replays are quiet.** A [checkpoint replay](sessions-and-checkpoints.md)
  of a completed run re-emits terminal events only — per-turn hooks and
  spans don't fire again; usage still folds into the caller.

## See also

- [Streaming](streaming.md) — the full event catalog hooks receive
- [Cancellation & steering](cancellation.md) — the control half of `ctx`
- Example: [`11_hooks.py`](../../examples/11_hooks.py)
