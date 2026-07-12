# Core concepts

Six ideas carry the whole framework. Each exists because something concrete
breaks without it; this page introduces them by the problem they solve, then
walks one run end to end so the rest of the docs can assume the vocabulary.

The six, in sixty seconds:

- **Agent vs Runner** — an `Agent` is immutable configuration; a `Runner`
  executes one run. Nothing about a conversation lives on the agent.
- **Run vs turn** — a run is the complete execution of one input; a turn is
  one iteration inside it: one model call followed by the tool calls it
  requested. One run may contain many turns.
- **Tools** — typed functions give the model capabilities beyond generating
  text. lovia derives their schemas, validates calls, and feeds results back
  into the run.
- **Transcript vs view** — the transcript is the append-only record of what
  happened; the view is what one model call gets to see. Long chats survive
  because only the view shrinks.
- **Session vs checkpoint** — a session is conversation memory *across* runs;
  a checkpoint is crash recovery *within* one run.
- **Plugins** — reusable capabilities contribute tools, instructions, view
  injectors, hooks, and guardrails through one extension mechanism, while the
  run loop retains control.

## The cast

```python
from lovia import Agent, Runner

agent = Agent(name="writer", instructions="Lead with the conclusion, then give one actionable next step.", model="<model>")
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

The problem this design solves: agent loops accrete special cases (approval
here, retry there, persistence somewhere else) until nobody can say what
happens in what order. lovia's answer is one loop with fixed phases. This is
the actual order of events; every guide hangs off some step of it.

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

## Tools: capabilities the model can invoke

A **tool** is a typed Python function exposed to the model. Add tools with
`Agent(tools=[...])`; lovia turns each signature into JSON Schema, validates
model-supplied arguments before invoking your code, and records both the call
and its result in the transcript.

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
- **The transcript is never rewritten.** Compaction shapes views; sessions
  and checkpoints only append; a completed run is immutable.
- **Plugins contribute; the loop controls.** No plugin can retry, abort, or
  reroute a run.
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
