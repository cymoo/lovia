# Core concepts

Five ideas carry the whole framework. Each exists because something concrete
breaks without it; this page introduces them by the problem they solve, then
walks one run end to end so the rest of the docs can assume the vocabulary.

The five, in sixty seconds:

- **Agent vs Runner** — an `Agent` is immutable configuration; a `Runner`
  executes one run. Nothing about a conversation lives on the agent.
- **Turns** — a run is a loop of turns: one model call, then the tool calls
  it requested. The loop ends when the model answers without tool calls.
- **Transcript vs view** — the transcript is the append-only record of what
  happened; the view is what one model call gets to see. Long chats survive
  because only the view shrinks.
- **Session vs checkpoint** — a session is conversation memory *across* runs;
  a checkpoint is crash recovery *within* one run.
- **Posture vs limits** — how the agent behaves under infrastructure trouble
  is agent configuration; how much one request may spend is a per-run
  argument.

## The cast

```python
from lovia import Agent, Runner

agent = Agent(name="writer", instructions="Be concrete.", model="openai:gpt-5.5")
result = await Runner.run(agent, "Draft a release note.")
```

**`Agent`** is a declarative dataclass: name, instructions, model, tools,
plugins, policies. It holds no conversation state, so one instance serves
any number of concurrent runs, and `agent.clone(model="...")` derives a
variant without copying anything mutable. (The one sanctioned in-place
mutation is registering dynamic instruction fragments with
`@agent.instruction` — see [Agents](agents.md).)

**`Runner`** is stateless — three static methods (`run`, `run_sync`,
`stream`) that translate arguments into one run. All mutable state for a run
lives inside the loop, created at start and gone at the end.

**`RunResult`** is what you get back: `output` (text, or your validated
`output_type`), `usage`, `turns`, `finish_reason`, `final_agent` (who was
active at the end — relevant after handoffs), and `entries` — the run's
**own** transcript contribution, not the whole conversation.

## One run, turn by turn

The problem this design solves: agent loops accrete special cases (approval
here, retry there, persistence somewhere else) until nobody can say what
happens in what order. lovia's answer is one loop with fixed phases. This is
the actual order of events; every guide hangs off some step of it.

**Setup, once per run:**

1. Resolve the active agent: providers, structured output, workspace
   session, plugin `setup()` (once per plugin), and the merged tool set —
   agent tools, plugin tools, workspace tools, handoff tools, and finally
   context-policy tools like `recall_tool_result` (added last; an explicit
   tool with the same name wins).
2. Build the transcript: `[system prompt] + prior session history + your
   input`. The system prompt concatenates the agent's instructions (plus
   dynamic fragments and any per-run `extra_instructions`), workspace
   instructions, plugin instructions, and — for providers without native
   JSON-schema support — the structured-output contract.
3. Run **input guardrails** once against the built transcript.

**Then the loop. Each iteration is one turn:**

1. Check limits: `max_turns`, cancellation, budget.
2. `TurnStarted` fires; queued **mailbox** messages drain into the
   transcript as user entries (this is how mid-run steering lands).
3. The **context policy** renders this call's view of the transcript;
   plugin **view injectors** append their transient entries (todo
   reminders and the like — never persisted).
4. The provider streams the model's reply: text deltas, reasoning deltas,
   tool-call deltas. On a context-overflow error with nothing yet streamed,
   the policy gets one chance to shrink the view and the call is retried.
5. The reply's entries append to the transcript; the checkpoint (if any)
   saves.
6. If the model requested tools: each call is **preflighted in order**
   (budget, approval, argument validation), then executed — concurrently
   where tools allow it, serially where they don't. Results append as they
   complete; each one is checkpointed.
7. If the model answered without tool calls: parse the final output. A
   structured-output parse failure arms one **repair** turn instead of
   failing (configurable).
8. `TurnEnded` fires. A pending **handoff** swaps the active agent (new
   system prompt, same conversation body) and the loop continues.

**On completion:** **output guardrails** run, the checkpoint is finalized,
and only then is the run's segment appended to the session — in that order,
so a crash can never leave a run both persisted and resumable. Every event
above is also dispatched to [hooks](observability.md) as it happens.

Streams have one more guarantee worth memorizing: **iterating a run's event
stream never raises.** Every stream closes with exactly one terminal event —
`RunCompleted` or `RunFailed` — and `await handle.result()` is where errors
become exceptions.

## Transcript vs view

The problem: conversations outgrow context windows, and most frameworks
"fix" this by rewriting history — after which nobody can audit what the
model actually saw, and resumed runs diverge.

lovia separates the two roles:

- The **transcript** is the canonical, append-only record: typed
  `TranscriptEntry` values (input, assistant text, reasoning, tool call,
  tool result) that preserve everything providers emit. Sessions and
  checkpoints persist the transcript. It only ever grows.
- The **view** is what one model call receives. The context policy (default:
  `Compaction`) may offload a huge tool result, clear old ones, or summarize
  ancient history — *in the view only*. The transcript is untouched, and a
  `recall_tool_result` tool lets the model retrieve anything the view
  dropped.

So "the model forgot" and "the record lost it" become different questions
with different answers. Details in [Context management](context.md).

## Session vs checkpoint

Two persistence stores that are easy to conflate and importantly different:

| | Session | Checkpoint |
| --- | --- | --- |
| Answers | "what has this conversation said so far?" | "how far did this run get?" |
| Keyed by | `session_id` (yours: user id, thread id, ...) | `run_id` (globally unique per checkpointer) |
| Holds | one segment per **completed** run | the one run that may still resume |
| Written | once, when a run completes | after the model turn and after every tool result |
| Lifetime | the conversation's | the run's (optionally deleted on success) |

Both are **append-only**: a stored run is never rewritten. The full
conversation at any moment is `session.load()` plus the in-flight snapshot's
entries. Re-issuing a completed `run_id` replays the stored result without
calling the model — that's what makes `run_id` an idempotency key, and it's
why crashed workers can simply retry their whole job. See
[Sessions & checkpoints](sessions-and-checkpoints.md).

## Posture vs limits

The problem: reliability knobs sprawl until every call site sets twelve
parameters. lovia's placement rule:

- **Posture** — how the agent behaves when infrastructure hiccups — lives on
  the `Agent` and is inherited by every run: provider `retry`, the
  `model=[...]` fallback chain, `default_tool_retries` /
  `default_tool_timeout`, `context_policy`.
- **Limits** — how much one request may spend — are `Runner.run` arguments
  with no agent-side counterpart: `max_turns`, `budget`, `cancel_token`.

One consequence worth knowing: the *initial* agent's posture governs the
whole run, handoffs included. A transfer changes who speaks, not the run's
spine. Per-call overrides exist for posture too
(`Runner.run(..., retry=..., context_policy=...)`) when one request really
is special. See [Reliability](reliability.md).

## RunContext: the one handle

Tools, hooks, guardrails, and dynamic instruction fragments all receive the
same live `RunContext`. A tool opts in by *type-annotating* its first
parameter — the name doesn't matter, the annotation does:

```python
from dataclasses import dataclass

from lovia import RunContext, tool


@dataclass
class Deps:
    db: "Database"


@tool
async def lookup(ctx: RunContext[Deps], user_id: int) -> str:
    """Fetch a user record."""
    return await ctx.deps.db.fetch(user_id)
```

| Field | What it is |
| --- | --- |
| `deps` (alias `context`) | the object you passed as `Runner.run(..., context=...)` |
| `entries` | the live transcript — treat as read-only |
| `messages` | chat-format view of `entries`, derived fresh on each access |
| `agent` | the currently active agent (changes on handoff) |
| `usage` | cumulative token usage so far |
| `turn` | 1-based index of the turn in flight |
| `session_id` / `run_id` | the run's persistence keys (`None` when unused) |
| `budget` | the run's `RunBudget`, for tools that want to self-throttle |
| `workspace` | the active agent's live workspace session, if any |
| `cancel_token` | always present — a tool or hook can request cancellation |
| `mailbox` | always present — push a message and the model sees it next turn |
| `system_prompt` | the fully rendered system prompt this run is using |

## Plugins: the one extension axis

The problem: frameworks grow a hook forest — one registry for tools, another
for prompt fragments, a middleware stack, lifecycle callbacks — and every
reusable capability needs all of them wired separately.

A lovia **plugin** is one object that contributes any mix of: tools, system
prompt text, per-turn view injectors, hooks, and guardrails. The runner
activates it once per run (`await plugin.setup()`), tears it down at run end,
and merges its contributions into the fixed loop slots above. Plugins never
drive control flow — the loop keeps the abort, the retry, and the handoff.

Skills, MCP, the todo list, and long-term memory are all plugins built on
exactly this seam, which is the proof it suffices. See [Plugins](plugins.md).

## When things go wrong

Every framework exception inherits `LoviaError`, so `except LoviaError`
catches lovia without catching your bugs. Errors carry an optional `hint` —
a one-line "what to try next" appended to the message.

| Exception | Raised when |
| --- | --- |
| `UserError` | the framework is misconfigured (no model, bad option) — fix the call site |
| `ProviderError` | the model API failed; carries `vendor`, `status_code`, `retryable` |
| `ContextOverflowError` | the prompt exceeds the context window and compaction couldn't save it |
| `ToolError` | a tool failed in a way worth structuring (yours to raise) |
| `InvalidToolArguments` | tool arguments failed schema validation (surfaced to the model to fix) |
| `OutputValidationError` | the final answer doesn't parse as `output_type` (after any repair) |
| `MaxTurnsExceeded` | the loop hit `max_turns` without a final answer |
| `BudgetExceeded` | a `RunBudget` limit tripped mid-run |
| `RunCancelled` | a `CancelToken` was tripped |
| `GuardrailTripped` | an input/output guardrail rejected a value |
| `MCPError` | an MCP server connection or call failed |

Two nuances: a tool raising an ordinary exception does **not** end the run —
the error is rendered back to the model as the tool result so it can adapt
(see [Tools](tools.md)); and in streaming mode these exceptions surface
through `handle.result()`, never through iteration.

## Design constraints you can rely on

The philosophy ("concise, lightweight, extensible, general-purpose") cashes
out as invariants you can build against:

- **Agents are configuration.** No conversation state on the `Agent`; safe
  to share, cheap to clone.
- **The transcript is never rewritten.** Compaction shapes views; sessions
  and checkpoints only append; a completed run is immutable.
- **Plugins contribute; the loop controls.** No plugin can retry, abort, or
  reroute a run.
- **Everything correlates by id, not position.** Tool events pair by
  `call.id`; segments and snapshots pair by `run_id`. Concurrency reorders
  nothing that matters.
- **Providers are protocols.** Two built-in adapters speak OpenAI and
  Anthropic over `httpx`; a third is an implementation of a `Protocol`, not
  a subclassing project.
- **The core stays small.** `lovia` imports without the web stack or any
  workspace machinery; those layers depend on the core, never the reverse.

## See also

- [Quickstart](quickstart.md) — the ten-minute path that motivated all this
- [Running agents](running.md) — the full `Runner` surface
- [Architecture notes](../architecture.md) — the contributor-level version
  of this page, with module names and invariants for people changing lovia
  itself
