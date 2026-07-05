# Tools

A tool is a typed Python function the model can call. lovia derives the JSON
Schema from the signature, validates arguments before your code runs, and
handles the loop mechanics — concurrency, retries, timeouts, truncation — so
a tool stays an ordinary function.

```python
from typing import Annotated

from pydantic import Field

from lovia import tool


@tool
async def lookup_order(order_id: str) -> str:
    """Look up an order by id."""
    return f"{order_id}: shipped"


@tool(strict=True)
def search_docs(
    query: Annotated[str, "Search terms"],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """Search internal documentation."""
    return []
```

Attach tools with `Agent(tools=[...])`. Sync functions run on a worker
thread so they never block the event loop; `async def` functions are awaited
directly.

## Schema derivation

The model sees `name`, `description`, and a JSON Schema for the parameters:

- **name** — the function name, unless `@tool(name=...)` overrides it.
- **description** — the docstring (or `@tool(description=...)`). This is
  the model's only guidance on *when* to call the tool — write it for the
  model, not for colleagues.
- **parameters** — from type hints. Defaults make arguments optional;
  `Annotated[T, "text"]` adds a plain-string description;
  `Annotated[T, Field(...)]` carries full Pydantic constraints (bounds,
  patterns, descriptions). Pydantic models, dataclasses, `TypedDict`s,
  literals, and unions all work as argument types.
- **`strict=True`** — marks the schema `additionalProperties: false` and
  every argument required, matching OpenAI's strict mode.

Arguments are validated (and coerced) against the signature before your
function runs. Invalid arguments never reach your code: they become an
`InvalidToolArguments` error result carrying a compact validation message,
which the model uses to correct the call — deterministic failures are not
retried.

## Receiving the run context

Annotate one parameter as `RunContext` (any name) and the runner injects the
live per-run handle — dependencies, transcript, usage, mailbox, cancel
token:

```python
from lovia import RunContext, tool


@tool
async def save_note(ctx: RunContext, text: str) -> str:
    """Save a note for this conversation."""
    await db.save(ctx.session_id, text)
    return "saved"
```

The context parameter is excluded from the schema the model sees. At most
one parameter may carry the annotation (`UserError` otherwise). The full
field catalog is in [Core concepts](concepts.md#runcontext-the-one-handle).

## Error semantics

A tool raising an exception does **not** end the run. The runner catches it,
renders a `"Tool error: ..."` string as the call's result, and lets the model
adapt — retry differently, pick another tool, or explain. Raise `ToolError`
(optionally with `hint=`) when you want to shape that message deliberately.

Three exceptions are special:

- `InvalidToolArguments` — deterministic; becomes an error result without
  retries (see above).
- `RunCancelled` — a run-global signal; re-raised, ending the run.
- `BudgetExceeded` — scoped: raised *by* the run's own budget it ends the
  run at the next safe point, but raised inside a delegated
  [agent-as-tool](multi-agent.md#agent-as-tool) sub-run it is a recoverable
  delegation failure and becomes an error result.

## Parallel execution and barriers

When the model requests several calls in one turn, they **execute
concurrently by default**. Order-sensitive tools opt out:

```python
@tool(parallel=False)
async def apply_migration(name: str) -> str:
    """Apply a database migration (never concurrently with other tools)."""
    return "applied"
```

`parallel=False` makes the call an **execution barrier**: every in-flight
call of the turn finishes first, the tool runs alone, then the remaining
calls proceed. A turn consisting only of barrier tools reproduces the
serial loop exactly.

The details that matter in practice:

- [Handoff](multi-agent.md) tools are always barriers, whatever their flag —
  that's what keeps "the first handoff of a turn wins" race-free. The
  built-in workspace mutators (`write_file`, `edit_file`, `shell`) default
  to `parallel=False`; read-only tools stay parallel.
- Preflight — budget checks, approval, argument validation — always runs
  serially in request order, so approval prompts and budget accounting are
  deterministic even while execution is concurrent.
- Results are checkpointed as they complete and appended to the transcript
  in **completion order**; everything downstream pairs calls to results by
  `call_id`, so ordering is cosmetic.
- Stream events of different calls interleave — correlate by `ev.call.id`
  ([Streaming](streaming.md#tools-and-approval)).
- `parallel=` controls *execution*. The request-side twin — whether the
  model may *emit* several calls per turn — is
  `ModelSettings.parallel_tool_calls` ([Providers](providers.md#modelsettings)).

## Retries and timeouts

```python
@tool(retries=2, timeout=10.0)
async def flaky_lookup(key: str) -> str:
    """Fetch from a service that sometimes blips."""
    ...
```

- `retries` — attempts after the first (default `0`); exponential backoff
  capped at 5s between attempts. `None` inherits the agent's
  `default_tool_retries`.
- `timeout` — per-attempt seconds; `None` inherits the agent's
  `default_tool_timeout` (default: no timeout).
- Cancellation, budget exhaustion, and invalid arguments are never retried —
  none of them can clear on their own.

## Output truncation

Rendered tool output is capped before it enters the transcript:
per-tool `@tool(max_output_chars=...)`, else the agent's
`max_tool_output_chars` (default **200,000 chars** — a tripwire for runaway
payloads, not a policy). Longer output is truncated head + tail with a
marker stating how much was cut, and the raw return value is dropped.

This is deliberately lossy — it bounds memory, checkpoint, and session cost
at the source. `recall_tool_result` sees the truncated version too; a tool
whose full output must survive should write it to the
[workspace](workspace.md) and return the path. (Distinct from
[context compaction](context.md), which is lossless and view-only.)

## Result renderers

The model receives a string. By default: strings pass through, everything
else is JSON-serialized (Pydantic models, dataclasses, enums, dates, paths
all handled). Override per tool or per agent:

```python
@tool(result_renderer=lambda rows, ctx: format_as_markdown_table(rows))
async def top_customers(n: int = 10) -> list[dict]: ...
```

Resolution order: the tool's `result_renderer`, else the agent's
`tool_result_renderer`, else the default. Renderers see **successful**
results only — the runner's `"Tool error: ..."` strings bypass them. The
raw, un-rendered value still reaches observers via
`ToolCallCompleted.result`.

## Tool policies

For cross-cutting behavior around a *single attempt* — caching, redaction,
rate limiting, custom auth — compose `ToolPolicy` callables instead of
wrapping functions by hand:

```python
async def cache_policy(invoke, args, ctx):
    key = ("search_docs", tuple(sorted(args.items())))
    if key in cache:
        return cache[key]
    result = await invoke(args, ctx)
    cache[key] = result
    return result


@tool(policies=[cache_policy])
async def search_docs(query: str) -> list[str]: ...
```

A policy receives `(invoke, args, ctx)` — the next callable in the chain,
the **raw** (not yet validated) arguments, and the run context. It may
mutate arguments, short-circuit, loop internally, or transform the result.
Policies compose in list order (first = outermost); framework retries and
the timeout wrap the *whole* chain, so each policy sees one attempt at a
time. Argument validation happens innermost, at the function boundary — a
policy that needs coerced values validates for itself.

For gating that needs a *human decision* rather than code, use
`needs_approval` — see [Human in the loop](human-in-the-loop.md).

## Building tools programmatically

`@tool` is a convenience over the `Tool` dataclass (`name`, `description`,
`parameters`, `invoke`, plus the policy fields above). Factories that close
over configuration return `Tool` values — the built-in
`web_search(impl)` and `ask_human(channel)` are examples; a
[plugin](plugins.md) is the packaging for tools that come with prompt text
or lifecycle.

## Sharp edges

- **Tool names must be unique per agent** across every source — agent
  tools, plugins, workspace, handoffs. A conflict raises `UserError` at run
  start (MCP servers prefix their tools for exactly this reason).
- **A cancelled sync tool finishes anyway.** Cancellation cannot interrupt
  a worker thread; the call's effects may still happen after the run ends.
  Prefer `async def` for anything long or side-effecting.
- **Policies see raw arguments.** Defaults are not applied and types are
  not coerced yet — treat `args` as untrusted model output at that layer.
- **The truncation cap is per rendered result, in characters.** 200k chars
  ≈ 50k tokens; if your tool can legitimately return more, raise the cap or
  persist the payload elsewhere — silent middle-loss is worse than either.

## See also

- [Built-in tools](built-in-tools.md) — HTTP, search, time
- [Human in the loop](human-in-the-loop.md) — approval gates
- [Plugins](plugins.md) — packaging tools with instructions and lifecycle
- Example: [`02_tools.py`](../../examples/02_tools.py)
