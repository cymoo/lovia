# Core concepts

Six groups of concepts recur throughout a Run. This page defines their
boundaries and follows them in execution order through the complete flow.

## Concepts at a glance

| Concept | Question it answers | Key distinction |
| --- | --- | --- |
| Agent and Runner | What is configured, and what executes it? | Agent stores configuration; Runner starts execution |
| Run and turn | How does one request progress? | A Run contains one or more turns |
| Tool | How does the model call external capabilities? | Schema, metadata, and async `invoke`, usually built with `@tool` |
| Transcript and view | What is recorded, and what does the model see? | The conversation body is canonical; a view is one model input |
| Session and Checkpoint | How is state persisted? | Session stores conversation; Checkpoint recovers a Run |
| Plugin | How is a capability bundle reused? | Plugins contribute; the run loop still controls execution |

## The cast

```python
from lovia import Agent, Runner

agent = Agent(name="writer", instructions="Explain user impact before implementation details.", model="<model>")
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

## Run vs turn

A **run** takes one input through the agent loop to a final result or failure.
A call to `Runner.run()` or `Runner.stream()` starts it; if interrupted, a
later call may resume the same run from a checkpoint. The input, accumulated
usage, limits, active agent, and transcript segment all belong to that run. A
handoff changes the active agent but does not start a new run.

A **turn** is one logical pass through the run loop: obtain one model response,
then execute any tools it requested. Tool execution is still part of that
turn; the next turn begins only when the model is called again with the
results. Transparent provider retries do not create extra turns. A model
response with no tool calls normally ends the run. Consequently, `max_turns`
limits logical model steps rather than raw HTTP attempts, and `RunResult.turns`
reports how many such iterations the run used.

## One run, turn by turn

Agent loops tend to accumulate special cases—approval here, retry there,
persistence somewhere else—until execution order becomes unclear. lovia keeps
them in one loop with fixed phases. From the user side, a Run has three parts:
prepare input, loop between the model and Tools, then validate and persist the
result. The steps below show the actual order.

### Setup, once per run

1. Resolve the active agent: providers, structured output, workspace
   session, plugin `setup()` (once per plugin), and the merged tool set —
   agent tools, plugin tools, workspace tools, and handoff tools.
2. Build the transcript: `[system prompt] + prior session history + your
   input`. The system prompt concatenates the agent's instructions (plus
   dynamic fragments and any per-run `extra_instructions`), workspace
   instructions, plugin instructions, and — for providers without native
   JSON-schema support — the structured-output contract.
3. Run **input guardrails** once against the built transcript.

### Each turn

1. Check limits: `max_turns`, cancellation, budget.
2. `TurnStarted` fires; queued **mailbox** messages drain into the
   transcript as user entries (this is how mid-run steering lands).
3. The **context policy** renders this call's view of the transcript;
   plugin **view injectors** append their transient entries (todo
   reminders and the like). These entries are never persisted: repeated
   injections neither disturb the stable prompt prefix (keeping provider
   caching effective) nor accumulate until the transcript balloons.
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

### On completion

**Output guardrails** run, the checkpoint is finalized,
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

lovia separates the in-memory transcript, persisted Run entries, and one
model call's view:

- The **full transcript** is “the active Agent's rendered system head + Session
  history + this Run's entries.” A Handoff replaces the system head with the
  target Agent's version; it does not rewrite the conversation body.
- **This Run's entries** contain input, assistant text, reasoning, Tool calls,
  and Tool results. They append only and form the body persisted in
  `RunResult.entries`, Session Segments, and Checkpoint Snapshots. Oversized
  Tool output may still be truncated before an entry is stored.
- The **view** is what one model call receives. The default `Compaction` policy
  may offload a huge Tool result, clear old ones, or summarize ancient history
  in the view only. `recall_tool_result` can retrieve content removed from the
  view.

So "the model forgot" and "the record lost it" become different questions
with different answers. Details in [Context management](context.md).

## Session vs checkpoint

Two persistence stores that are easy to conflate and importantly different:

| | Session | Checkpoint |
| --- | --- | --- |
| Answers | "what has this conversation said so far?" | "how far did this run get?" |
| Keyed by | `session_id` (yours: user id, thread id, ...) | `run_id` (globally unique per checkpointer) |
| Holds | one finalized Segment per Run; Runner auto-appends on success | one Run's in-flight or completed Snapshot for resume or replay |
| Written | once, when a run completes | after the model turn and after every tool result |
| Lifetime | the conversation's | the run's (optionally deleted on success) |

During normal execution, both stores append new Run records; compaction never
rewrites a stored Run. The full conversation at any moment is
`session.load()` plus the in-flight snapshot's entries. Re-issuing a completed
`run_id` replays the stored result without calling the model—that is what makes
`run_id` an idempotency key. See [Sessions & checkpoints](sessions-and-checkpoints.md).

Explicit Session maintenance may trim Tool results or rewind recent segments.
Checkpoint entries append, while its small status head is updated as the Run
advances. Neither operation is context compaction.

## Tools: capabilities the model can invoke

A **Tool** contains Schema, metadata, and an async `invoke`, and is usually
built by `@tool` from a callable's signature. Add it with
`Agent(tools=[...])`; lovia validates model-supplied arguments before invoking
your code and records both the call and its result in the Transcript.

```python
from lovia import Agent, tool


@tool
async def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"{order_id}: shipped"


agent = Agent(name="support", model="<model>", tools=[lookup_order])
```

When the model requests one or more tools, those calls and their results remain
part of the current turn. The following model call starts the next turn, where
the model can use the results to continue or answer. Tools may also come from
plugins, workspaces, and handoffs; names must be unique across the merged set.
See [Tools](tools.md) for schemas, concurrency, retries, approvals, and result
handling.

## RunContext: the one handle

Tools, hooks, guardrails, and dynamic instruction fragments receive the same
live `RunContext`. It exposes the current dependencies, Agent, transcript,
usage, persistence keys, Workspace, cancellation token, and Mailbox. A Tool
opts in by type-annotating a parameter — the parameter name does not matter:

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

Treat `ctx.entries` as read-only. Use `ctx.deps` for application dependencies,
`ctx.cancel_token` to request cancellation, and `ctx.mailbox` to steer the next
Turn. The complete field catalog is in the
[API reference](api-reference.md#runcontext).

## Plugins: the one extension axis

A reusable capability often needs Tools, instructions, per-turn reminders,
hooks, and cleanup together. A lovia **plugin** is one object that contributes any mix of: tools, system
prompt text, per-turn view injectors, hooks, and guardrails. The runner
activates it once per run (`await plugin.setup()`), tears it down at run end,
and merges its contributions into the fixed loop slots above. A Plugin does
not own control flow: it can affect a Run through Tools, Guardrails, and Hooks,
while the loop still executes aborts, retries, and handoffs.

Skills, MCP, the todo list, and long-term memory are all plugins built on
exactly this seam, which is the proof it suffices. See [Plugins](plugins.md).

## When things go wrong

Every framework exception inherits `LoviaError` and may carry a `.hint` with
the next action to try. Configuration problems raise `UserError`; provider,
context, validation, budget, cancellation, and guardrail failures use specific
subclasses so callers can recover narrowly. The full catalog is in the
[API reference](api-reference.md#exceptions), with symptom-driven fixes in
[Troubleshooting](troubleshooting.md).

Two rules matter immediately: an ordinary Tool exception becomes a result the
model can react to instead of ending the Run; and streaming failures surface
from `await handle.result()`, never while iterating events.

## Design constraints you can rely on

The philosophy ("concise, lightweight, extensible, general-purpose") cashes
out as invariants you can build against:

- **Agents are configuration.** No conversation state on the `Agent`; safe
  to share, cheap to clone.
- **Compaction never rewrites the conversation body.** Handoff replaces only
  the system head; persistence maintenance is separate and explicit.
- **Plugins contribute; the loop controls.** A Guardrail or Hook may request an
  abort, but only through extension points executed by the loop.
- **Everything correlates by id, not position.** Tool events pair by
  `call.id`; segments and snapshots pair by `run_id`. Concurrency reorders
  nothing that matters.
- **The core stays small.** The default install has only three runtime
  dependencies: `httpx`, `pydantic`, and `pyyaml`. Capabilities that need
  additional libraries, such as MCP and the web app, ship as opt-in extras
  and are imported only when used.

## See also

- [Quickstart](quickstart.md) — the ten-minute path that motivated all this
- [Running agents](running.md) — the full `Runner` surface
- [Architecture notes](../architecture.md) — the contributor-level version
  of this page, with module names and invariants for people changing lovia
  itself
