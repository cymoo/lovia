# Streaming

A UI can't wait for `RunResult` — it needs text as it generates, tool calls
as they start, approval requests the moment they block. `Runner.stream`
yields typed events for all of it, and the same event types drive
[hooks](observability.md), so learning the catalog once pays twice.

```python
from lovia import Runner, events

handle = Runner.stream(agent, "Explain context windows in one paragraph.")

async for ev in handle:
    match ev:
        case events.TextDelta(delta=d):
            print(d, end="", flush=True)
        case events.ToolCallStarted(call=c):
            print(f"\n[{c.name}...]", end="")
        case events.RunFailed(error=e):
            print(f"\nrun failed: {e}")

result = await handle.result()
```

Events are plain dataclasses in `lovia.events`. Filter with `isinstance` or
`match` — every event derives from a small base-class family
(`RunEvent`, `TurnEvent`, `DeltaEvent`, `MessageEvent`, `ToolEvent`,
`TransitionEvent`, `ErrorEvent`, `ContextEvent`) when you want a whole
category.

## The contract

Three guarantees shape every consumer:

1. **Iteration never raises for run failures.** Every stream ends with
   exactly one terminal event — `RunCompleted` or `RunFailed` — and then
   stops. Errors become exceptions only at `await handle.result()`. (Task
   cancellation and other `BaseException`s still propagate.)
2. **Deltas are provisional until `MessageCompleted`.** A transient
   mid-stream provider failure can discard partial output and restart the
   turn — see `OutputDiscarded` below.
3. **Tool events of one turn interleave.** Calls execute concurrently by
   default, so correlate events by `ev.call.id`, never by adjacency.

## Event catalog

### Run and turn lifecycle

| Event | Fields | When |
| --- | --- | --- |
| `RunStarted` | `agent` | once, before the first turn |
| `TurnStarted` | `agent`, `turn` | each turn, before the model call |
| `TurnEnded` | `agent`, `turn` | each turn, after tools finish |
| `RunCompleted` | `result` | terminal: the run succeeded |
| `RunFailed` | `error` | terminal: the run ended without a result |

### Model output

| Event | Fields | When |
| --- | --- | --- |
| `TextDelta` | `delta` | a fragment of assistant text |
| `ReasoningDelta` | `delta` | a fragment of chain-of-thought, for providers that expose it — render as collapsed/secondary text, never rely on it for behavior |
| `OutputDiscarded` | — | the turn's streamed deltas so far are void; clear what you rendered — a fresh stream follows |
| `MessageCompleted` | `entries` | one assistant turn fully assembled: the new `TranscriptEntry` values it produced |
| `UserMessageInjected` | `content`, `turn` | a [mailbox](reliability.md#steering-a-live-run) message was folded in as a user turn |

`OutputDiscarded` fires when the runner recovers from a mid-stream provider
error by retrying
([`RetryPolicy.restart_on_partial`](reliability.md#provider-retries)). The
persistent transcript is unaffected — it is assembled only from completed
turns.

### Tools and approval

| Event | Fields | When |
| --- | --- | --- |
| `ToolCallStarted` | `call` | just before a tool actually executes |
| `ToolCallCompleted` | `call`, `result`, `is_error`, `output` | the call reached a terminal outcome |
| `ToolCallFailed` | `error`, `call` | a non-terminal error scoped to one call (the run continues) |
| `ApprovalRequired` | `call`, `.approve()` / `.reject()` | a gated tool is waiting for a decision |

The fine print that UIs get wrong:

- A call rejected **before** execution — unknown tool, malformed arguments,
  denied approval — emits `ToolCallCompleted(is_error=True)` **without** a
  preceding `ToolCallStarted`. Don't assume the pair.
- `ToolCallCompleted.result` is the raw return value (for type-aware
  consumers); `.output` is the rendered string the model actually received.
- Completions arrive in **completion order**, not request order.
- `ToolCallFailed` carries the exception for observability; the model sees
  the paired `ToolCallCompleted(is_error=True)` string. Terminal run
  failures are `RunFailed`, never `ToolCallFailed`.
- Resolve `ApprovalRequired` by calling `ev.approve()` or `ev.reject()`
  before your loop yields control back to the runner — or later,
  out-of-band, via `handle.approvals`. Unresolved requests are **denied**
  when the turn needs the answer, so a forgetful UI can't hang a run. Other
  calls of the same turn keep executing while the stream sits at this
  event. Full decision flow in [Human in the loop](human-in-the-loop.md).

### Transitions and context

| Event | Fields | When |
| --- | --- | --- |
| `HandoffOccurred` | `from_agent`, `to_agent` | control [transferred](multi-agent.md) to another agent |
| `ContextCompacted` | `session_id`, `entries_before`, `entries_after`, `notice` | the [context policy](context.md) produced a compacted view for this turn |

`ContextCompacted.notice` is a JSON-safe `CompactionNotice` (reason,
reactive flag, token before/after, policy-authored `detail` lines, optional
summary) — the same object the web UI replays when a finished session is
reloaded.

## Patterns

**Progressive text with tool status** — the quickstart loop above; keep a
per-`call.id` map for concurrent tool spinners.

**Approval UI** — suspend on `ApprovalRequired`, show `ev.call.name` and
`ev.call.arguments`, call `ev.approve()`/`ev.reject()`:

```python
async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ok = await confirm_dialog(ev.call.name, ev.call.arguments)
        ev.approve() if ok else ev.reject()
```

**Server-side fan-out** — feed events into your own bus/SSE encoder; the
bundled [HTTP API](http-api.md) does exactly this, and
`lovia/web/sse.py` is a working reference for the translation.

**Observability without a UI** — the same events reach
[hooks](observability.md) even when nobody consumes the stream; prefer hooks
for metrics so instrumentation doesn't depend on who is iterating.

## Sharp edges

- **One iteration per handle.** A second `async for` raises `RuntimeError`;
  fan out downstream if several consumers need the events.
- **An abandoned stream is not a completed run.** Breaking out of the loop
  stops driving the run; `handle.result()` then reports abandonment instead
  of a result. Iterate to the terminal event (or use `await handle`) when
  you need the outcome.
- **Render deltas as provisional** until `MessageCompleted` — one
  `OutputDiscarded` and a naive UI shows the same paragraph twice.

## See also

- [Running agents](running.md) — the handle and result surface
- [Human in the loop](human-in-the-loop.md) — every way to resolve approvals
- [Observability](observability.md) — the same events, as hooks
- Example: [`03_streaming.py`](../../examples/03_streaming.py)
