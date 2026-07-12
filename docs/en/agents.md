# Agents

An `Agent` describes *what* should run — name, instructions, model, tools,
policies — while holding no conversation state, so one instance can serve any
number of concurrent requests and per-request variants are cheap clones
rather than copies of live state.

```python
from lovia import Agent, model_from_env

agent = Agent(
    name="writer",
    instructions="Write concrete, concise answers.",
    model=model_from_env(),
)
```

## The fields

Every field has a literal default — `None` never hides a constant; it means
off, inherit, or auto-created.

| Field | Default | What it does |
| --- | --- | --- |
| `name` | required | human-readable name; also derives handoff tool names (`transfer_to_<name>`) |
| `instructions` | `""` | base system prompt: a string, or a callable receiving the run's `RunContext` |
| `model` | `None` | `"vendor:model"` string or a `Provider` instance; running without one raises `UserError` |
| `tools` | `[]` | the [tools](tools.md) the model may call |
| `output_type` | `str` | typed final output; see [Structured output](structured-output.md) |
| `output_repair` | `True` | one repair prompt on a failed output parse; `False` fails fast; an `OutputRepairStrategy` customizes it |
| `handoffs` | `[]` | agents (or `Handoff` wrappers) the model may [transfer control to](multi-agent.md) |
| `settings` | `ModelSettings()` | sampling parameters forwarded to the provider |
| `retry` | `RetryPolicy()` | provider retry posture (4 retries, jittered backoff); `None` disables |
| `context_policy` | `Compaction()` | how the per-call view is shaped; see [Context management](context.md) |
| `workspace` | `None` | file/shell tools scoped by a policy; see [Workspace](workspace.md) |
| `plugins` | `[]` | capability bundles; see [Plugins](plugins.md) |
| `hooks` | `None` | an `AgentHooks` observing every run event; see [Observability](observability.md) |
| `approval_handler` | `None` | programmatic approval policy; see [Human in the loop](human-in-the-loop.md) |
| `input_guardrails` / `output_guardrails` | `[]` | checks that can stop a run; see [Guardrails](guardrails.md) |
| `default_tool_retries` | `0` | retries for tools that don't set their own |
| `default_tool_timeout` | `None` | per-attempt timeout for tools that don't set their own |
| `max_tool_output_chars` | `200_000` | transcript-size tripwire for runaway tool outputs (see [Tools](tools.md#output-truncation)) |
| `tool_result_renderer` | `None` | agent-wide renderer for tool results whose tool has none |

The reliability-shaped fields follow one rule worth internalizing —
**posture lives on the agent, limits live on the run** — covered in
[Reliability](reliability.md).

## Instructions

Four forms, composing from static to fully dynamic:

**A string** — the common case.

**A callable** — the whole base prompt becomes dynamic. It receives the
run's `RunContext` (the same handle tools get) and may be sync or async:

```python
async def instructions(ctx) -> str:
    return f"You support the {ctx.deps.plan} plan. Be brief."

agent = Agent(name="support", instructions=instructions, model=model_from_env())
```

**Registered fragments** — keep the base static and append dynamic pieces
with the `@agent.instruction` decorator. Fragments render in registration
order after `instructions`, separated by blank lines; returning `""` skips a
fragment conditionally:

```python
agent = Agent(name="support", instructions="You are a support agent.", model=model_from_env())

@agent.instruction
async def user_tier(ctx) -> str:
    return f"User tier: {ctx.deps['tier']}" if ctx.deps else ""
```

**`with_instructions`** — the purely functional variant: returns a clone
with one more fragment, leaving the original untouched.

The rendered result — base + fragments + any per-run `extra_instructions`
addendum — is what the model sees as its system prompt, observable
afterwards as `ctx.system_prompt`. Workspace instructions, plugin
instructions, and (for providers without native JSON-schema support) the
structured-output contract are appended after it by the runner.

> **Dynamic prompts and provider caches.** Providers cache the prompt
> prefix; a fragment whose text changes every call (timestamps, request
> ids) invalidates that cache each turn. Render stable text — dates rather
> than times (see `current_date` in
> [Built-in tools](built-in-tools.md#time)), tiers rather than session ids —
> and put volatile detail in tool results instead.

## Clones and variants

`clone()` returns a copy with selected fields replaced — the intended way to
derive per-request or per-experiment variants:

```python
strict = agent.clone(instructions="Answer with citations only.")
variant = agent.clone(model="<other-model>")
```

The boundary between `@agent.instruction` and `clone()` is
**copy-on-register**: fragments registered *before* a clone are carried into
it (as an immutable tuple — no shared mutable state); fragments registered
*after* affect only the agent they were registered on. Register fragments
right after constructing the agent, or use `with_instructions` when you'd
rather not mutate at all.

## Per-run dependencies

Anything your instructions, tools, hooks, or guardrails need at runtime — a
database pool, the current user — travels as the run's `context` object, not
as agent state:

```python
from dataclasses import dataclass

from lovia import Agent, RunContext, Runner, tool


@dataclass
class Deps:
    user_id: str
    db: "Database"


@tool
async def open_tickets(ctx: RunContext[Deps]) -> str:
    """List the user's open tickets."""
    rows = await ctx.deps.db.tickets(ctx.deps.user_id)
    return "\n".join(rows) or "No open tickets."


agent: Agent[Deps] = Agent(name="support", model=model_from_env(), tools=[open_tickets])

result = await Runner.run(agent, "Any open tickets?", context=Deps("u1", db))
```

The generic parameter (`Agent[Deps]`, `RunContext[Deps]`) is for your type
checker; at runtime `ctx.deps` is simply whatever you passed (or `None`).
Tools opt into receiving the context by *annotating* a parameter as
`RunContext` — the name doesn't matter, and at most one parameter may carry
the annotation. Everything else on the context handle — transcript, usage,
mailbox, cancel token — is catalogued in
[Core concepts](concepts.md#runcontext-the-one-handle).

## Running an agent

`agent.run(...)`, `agent.run_sync(...)`, and `agent.stream(...)` are thin
shortcuts for the corresponding `Runner` methods, which hold the full
parameter surface — sessions, budgets, checkpoints, steering. See
[Running agents](running.md).

## Sharp edges

- **`@agent.instruction` mutates the agent** — the one deliberate exception
  to immutability, kept for decorator ergonomics. If clones are involved,
  registration order relative to `clone()` decides who gets the fragment
  (copy-on-register, above).
- **Callable instructions run on every turn's prefix render, not once.**
  They must be fast and deterministic-ish; slow I/O in an instructions
  callable stalls every model call.
- **`Agent` is a plain dataclass** — nothing stops direct field assignment,
  but the framework assumes agents don't change mid-run. Treat instances as
  frozen; use `clone()`.

## See also

- [Running agents](running.md) — the full run/stream surface
- [Providers & models](providers.md) — what `model=` accepts
- Examples: [`01_hello.py`](../../examples/01_hello.py),
  [`18_dependencies.py`](../../examples/18_dependencies.py)
