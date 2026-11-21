# Context management

Long conversations outgrow model windows. lovia shrinks only the **view** sent
to the Provider, never the transcript or Session. Content leaving the current
view does not mean the system lost it.

```python
from lovia import Agent, Compaction

agent = Agent(
    name="companion",
    model="<model>",
    context_policy=Compaction(
        context_window=200_000,
        compact_at=0.85,
        compact_to=0.60,
    ),
)
```

Context policy is agent *posture* — set it once, every run inherits it;
`Runner.run(..., context_policy=...)` overrides one call. The default is
already `Compaction()` — you configure it when the defaults don't fit, and
disable it with `NoopContextPolicy`:

```python
from lovia.context import NoopContextPolicy

agent = Agent(..., context_policy=NoopContextPolicy())
```

| Situation | Recommendation |
| --- | --- |
| Most applications | Keep the default `Compaction()` |
| The endpoint window is known | Set `context_window` explicitly |
| Offloaded Tool output must remain available | Configure a `ResultStore` |
| An upstream layer already manages context | Use `NoopContextPolicy` |

## What Compaction does

Every turn, before the model call, the policy sizes the transcript against
the window and — under pressure — renders a smaller view in
**cheap-first stages**:

1. **Offload huge tool results** (`OffloadToolResults`, ≥4,000 chars):
   replaced in the view by a 400-char preview marker; the full output is
   archived to the [result store](#result-stores) when one is configured.
2. **Clear older tool results** (`ClearToolResults`): replaced by a short
   marker, keeping the newest few verbatim.
3. **Summarize old history** (`SummarizeHistory`): an incremental LLM
   summary replaces the oldest span — structured sections (session intent,
   current state, key facts, artifacts, constraints, next steps) rather
   than freeform prose.
4. **Truncate** (`TruncateHistory`, aggressive path only): when the
   provider has rejected the prompt — or the view is already over the
   window before sending — and the three stages above still leave it over
   the window — a summarizer that keeps failing, or a recent span too big
   to protect — the unsummarized span is dropped and a short note takes
   its place in the view. Lossy, but the alternative is a session that
   fails the same way on every later turn.

Markers preserve the pairing (`call_id`, error flag) and tell the model how
to get content back:

```
[Earlier tool result cleared to save context.
 Call recall_tool_result("call_42") to retrieve the full output.]
```

`recall_tool_result` is provided **automatically** by the policy — no
wiring. It reads the result store first and falls back to the transcript,
so recovery never re-runs a tool that had side effects.

Three guarantees shape the design:

- **Sticky decisions, stable prefix.** Stages record decisions
  (cleared ids, offload records, the running summary); the view is
  re-rendered from them each turn. Decisions are monotonic, so the rendered
  prompt prefix stays byte-stable across turns — which is what keeps
  [provider prompt caches](providers.md#prompt-caching) warm. Compaction
  and caching are allies, not enemies.
- **A protected tail.** The most recent span (default: 20% of the usable
  window, at least the latest user message and always whole
  call/result pairs) is never compacted — the model always sees its
  immediate context verbatim.
- **Reactive backstop.** If the provider still rejects the prompt
  (`ContextOverflowError`), the policy gets one shot at a more aggressive
  view (thresholds dropped, oversized results in the tail offloadable,
  truncation allowed) and the turn is retried — only when the rebuilt view
  is meaningfully smaller. How hard it shrinks depends on whether the
  estimate explains the overflow: if the prompt was over a window now known
  (usually the one the error named), it compacts to the normal `compact_to`
  of that window; if not, the count itself was wrong, so it overshoots —
  tail 10%, target ~25% of the failed prompt. A view whose estimate is
  certainly over a known window before sending — more than 1.4× it, past
  what an uncalibrated estimate over-counts (typically this turn's own huge
  tool result) — gets an ordinary pass first and, if it is still that far
  over, the first form up
  front — the same pass the refusal would have triggered, without sending
  it just to be refused. With
  the default stages the error surfaces only when the latest user turn alone
  exceeds the window; its hint says what to do.

Each compaction emits a
[`ContextCompacted` event](streaming.md#transitions-and-context) with a
`CompactionNotice` (reason, token before/after, human-readable detail) —
the web UI renders these live and replays the last one on reload.

## Configuration

```python
Compaction(
    context_window=None,        # tokens; None = ask the provider
    compact_at=0.85,            # trigger watermark
    compact_to=0.60,            # target after compaction
    keep_recent_tokens=None,    # protected tail; None = usable // 5
    reserve_output_tokens=16_384,
    stages=None,                # your own pipeline; None = the four above
    summarizer=None,            # your own Summarizer; None = LLMSummarizer()
    image_tokens=1_600,         # flat estimate per image part
    store=None,                 # ResultStore for offloaded outputs
)
```

- **Watermarks** accept a fraction of the usable window (`0.85`) or an
  absolute token count (`150_000`); "usable" = window −
  `reserve_output_tokens`. Nothing happens below `compact_at`; a breach
  shrinks the view to `compact_to` (hysteresis, so the policy doesn't
  thrash at the boundary).
- **`context_window=None`** resolves the window from the endpoint — its
  `/models` listing, or the limit it names the first time it rejects a
  prompt. See
  [context windows](providers.md#context-windows) for the full chain. A
  window it names always caps yours, so an unlisted model costs one
  overflow and is then sized correctly for the rest of the session. Only
  when *nothing* can report it is proactive compaction skipped and the
  reactive overflow path left as the sole backstop.
- **Token accounting** is a calibrated estimate: UTF-8 bytes/4
  heuristics — so CJK text weighs in proportionally instead of being
  under-counted 4× — with flat costs for images/files, plus the tool
  schemas the request carries (a fixed additive payload that would
  otherwise poison the multiplier), corrected by an EMA against the
  provider's *real* input-token counts as turns complete — one ratio per
  model, so a mid-session model switch doesn't inherit another tokenizer's
  correction. A run's final count carries into the next run on the session,
  so plain chat (one call per run) calibrates too. Providers can
  supply exact counting by implementing `TokenEstimator`. In live calibration
  across English, Chinese, mixed-language, and code-heavy transcripts, the
  calibrated estimate reached roughly **99% accuracy** (about 0.7–1.3% steady
  error); see the [calibration report](https://github.com/cymoo/lovia/blob/main/docs/ratio-calibration.md).

## Result stores

Offloaded outputs need somewhere to live if they should outlast the view:

```python
from lovia.context import Compaction, FileResultStore

policy = Compaction(context_window=200_000, store=FileResultStore(".cache/results"))
```

`ResultStore` is two methods (`put(key, content)` / `get(key)`), keyed by a
**content digest** of the output — the store is shared across sessions while
call_ids are session-local, so digest keys make cross-session collisions
impossible (and dedupe identical outputs for free); the offload marker hands
the model the digest as its recall reference. `FileResultStore(dir)` writes one file per result (no eviction —
retention is yours); `InMemoryResultStore(max_entries=1024)` is a bounded
LRU. Without a store, offload markers still work — recall falls back to the
transcript — but a [session `trim_tool_results`](sessions-and-checkpoints.md#maintenance)
later would truncate what was never archived.

## State: where decisions live

Sticky decisions (cleared ids, offload previews, summary + coverage, the
calibration ratio) serialize into the run's checkpoint and, at run end,
into the session segment's `meta` — so the *next* run on the same session
resumes prior decisions instead of re-deriving them, and a
[resumed run](sessions-and-checkpoints.md) picks up exactly where it
compacted. A structural fingerprint of the summarized prefix detects a
rewritten history (e.g. trimmed offline) and resets the summary while
keeping the id-keyed decisions.

## Custom policies and stages

Two extension depths. **Custom stages** keep Compaction's machinery
(watermarks, tail, state, markers) and replace what gets compacted:

```python
class DropOldImages:                      # implements Stage
    name = "drop_images"
    async def plan(self, body, ctx) -> bool:
        ...   # record decisions into ctx.state; return True if anything new
```

```python
policy = Compaction(stages=[DropOldImages(), ClearToolResults()])
```

Stages *plan* (record sticky decisions); rendering is a pure function of
transcript + state. Never make a stage undo a decision — monotonicity is
what keeps the prefix cache-stable. A stage's `ctx` is a `StageContext`
(the request, the sticky `CompactionState`, a `TokenCounter`, the
`TokenBudget`, the protected-tail boundary, the aggressive flag — set after
an overflow, and before sending a view still over the window after an
ordinary pass). The pieces Compaction itself is built from are exported for
reuse —
`render_view`, the `clear_marker` / `offload_marker` / `summary_entry`
builders, `transcript_to_text`, `OffloadRecord` / `SummaryState`, and the
summarizer's `REQUIRED_SECTIONS` / `SUMMARY_SYSTEM_PROMPT` /
`SUMMARY_WRAPPER` templates (customize `LLMSummarizer(prompt=...,
required_sections=...)` rather than forking it).

**A custom `ContextPolicy`** replaces everything: one method,
`async compact(req: CompactionRequest) -> ContextResult`. The request
carries the entries (read-only), the provider, `last_input_tokens`, the
`overflow` flag, `reported_window` (the limit the endpoint named while
rejecting the last prompt — remember it, it outranks every other window
source), a `scratch` dict the runner round-trips through checkpoints for
you, and a `usage` accumulator: add the spend of any model call the policy
makes itself and the runner folds it into the run's usage and budget.
Return the view plus `changed`/`compacted` flags, optional token
counts, and an optional `window` — the window the call was budgeted
against, which the runner forwards as `MessageCompleted.context_window`
(the web UI's context ring reads it). Set `calibrates=True` only when
`scratch` now holds your estimate of exactly the returned view: the runner
then carries the real count of the call that sends it into the next run on
the session, as that run's first `last_input_tokens`. An optional `tools()` method contributes tools —
`recall_tool_result(store)` from `lovia.tools` is the factory
`Compaction` uses to ship recall, reusable by any policy that drops
content. `lovia/context/policy.py` is a one-screen read.

## Sharp edges

- **Compaction is not a memory cap.** The *transcript* keeps full outputs;
  only the view shrinks. Runaway payloads are bounded at the source by
  [tool output truncation](tools.md#output-truncation) — that one is lossy,
  by design, and `recall_tool_result` sees the truncated version.
- **Summaries cost a model call** on the run's own provider (temperature
  0). Repeated summary failures trip a per-run circuit breaker (the
  aggressive path stays as the half-open probe; truncation covers for it
  there), and a summary that wouldn't save ≥10% is skipped. The spend is on the books — it counts in
  `RunResult.usage` and against the `RunBudget`, checked before the turn's
  main call goes out — but budget-sensitive deployments should know turn N
  can contain a hidden LLM call.
- **The first overflow on an unknown model is a real, failed request.**
  The endpoint's rejection is what teaches lovia the window; the retry is
  then sized against it like a proactive compaction, so a long history
  recovers in that one retry. Endpoints whose error names no limit
  (llama.cpp) get the overshoot instead. Set `context_window=...` up front
  where you know it. Ollama never overflows
  at all (it [truncates silently](providers.md#sharp-edges)), so there it
  is not optional.
- **Don't share one `Compaction` across agents with different windows** —
  state is per run/session, but the configured window is the instance's.
  Cloning agents shares the policy instance; give variants their own.

## See also

- [Concepts: transcript and view](concepts.md#transcript-vs-view)
- [Providers](providers.md#context-windows) — window reporting and caching
- [Sessions & checkpoints](sessions-and-checkpoints.md) — carried state
- Example: [`17_context_compaction.py`](../../examples/17_context_compaction.py)
