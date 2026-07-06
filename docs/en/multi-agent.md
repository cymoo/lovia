# Multi-agent

Multi-agent composition in lovia is deliberately atomic: two primitives, both
implemented as ordinary tools, and no orchestration DSL. **Handoff**
transfers control — the specialist continues the same conversation.
**Agent-as-tool** delegates — a sub-agent answers a bounded question and the
parent carries on. Everything larger is composed from these in plain Python.

## Handoff

```python
from lovia import Agent, Runner

billing = Agent(name="billing", instructions="Handle billing issues.", model="glm-5.2")
support = Agent(name="support", instructions="Handle technical issues.", model="glm-5.2")

triage = Agent(
    name="triage",
    instructions="Route the user to the right specialist.",
    model="glm-5.2",
    handoffs=[billing, support],
)

result = await Runner.run(triage, "I was charged twice.")
print(result.final_agent.name)   # "billing"
```

Each entry in `handoffs` becomes a `transfer_to_<name>` tool (names are
slugified to fit provider grammars — ASCII, 64 chars, with a stable digest
suffix when needed). When the model calls one, the loop swaps the active
agent and continues.

**What the target sees.** The conversation follows the handoff: the new
agent gets the full prior context — history, tool calls and results
included — with exactly one change: the leading system prompt is re-rendered
as the target's own (its instructions, workspace, plugins,
structured-output contract). A run-level `extra_instructions` addendum is
re-applied to every agent a handoff reaches.

**What else changes.** The target agent's providers, tools, plugins, and
workspace are resolved fresh (plugins run their own `setup()`); a
`HandoffOccurred` event fires (dispatched to both agents' hooks). What does
*not* change: the run's spine — `max_turns`, budget, cancel token, mailbox,
session, checkpoint, and the *initial* agent's retry/context posture all
carry through.

### Customizing a handoff

Wrap the target in a `Handoff` for control over the tool:

```python
from lovia import Agent, Handoff

triage = Agent(
    name="triage",
    model="glm-5.2",
    handoffs=[
        Handoff(
            target=billing,
            description="Billing: refunds, double charges, invoices, payment methods.",
            on_handoff=lambda args, ctx: audit_log(ctx.session_id, args.get("reason")),
        ),
        support,   # plain agents still work alongside
    ],
)
```

| Field | Default | Purpose |
| --- | --- | --- |
| `target` | required | the agent to transfer to |
| `name` | `transfer_to_<slug>` | override the tool name |
| `description` | generic transfer text | **the routing signal** — set it to the target's specialty whenever the parent must choose between similar agents |
| `on_handoff` | `None` | sync or async callback `(args, ctx)` fired when the handoff triggers; `args` carries the model's optional `reason` |

The default description is deliberately thin (just the agent name), so
`description` is the knob that makes routing reliable.

### Handoff semantics worth knowing

- **First handoff wins.** Handoff tools always execute as
  [barriers](tools.md#parallel-execution-and-barriers) — never concurrently
  with other calls — and a second handoff in the same turn is rejected
  *before* its `on_handoff` side effects fire.
- **The swap happens at turn end.** Remaining tool results of the turn are
  processed first; the next model call is the target's.
- **Resume survives handoffs.** A [checkpoint](sessions-and-checkpoints.md)
  records the *active* agent by name; resuming resolves it from the entry
  agent's handoff graph and continues as that agent.
- **Handoffs don't nest runs.** However deep a chain of transfers goes, it
  is still one run, one transcript, one budget.

## Agent-as-tool

Delegation instead of transfer — the sub-agent runs in its own loop, sees
only the prompt it was handed (never the parent's history), and its final
output comes back as the tool result:

```python
summarizer = Agent(
    name="summarizer",
    instructions="Summarize text in five bullets.",
    model="glm-5.2",
)

manager = Agent(
    name="manager",
    instructions="Delegate summarization when useful.",
    model="glm-5.2",
    tools=[summarizer.as_tool(description="Summarize a passage.")],
)
```

`agent.as_tool(*, name=None, description=None, max_turns=50, budget=None,
retry=None, context_policy=None)`:

- The tool is named `ask_<slug>` by default and takes one model-controlled
  argument: `input`, the delegated prompt. The execution-policy keywords are
  fixed by *you* and never exposed to the model — bound `max_turns`
  especially, since a delegated agent loops on its own.
- `budget` is copied per invocation, so its limits apply to each sub-run
  individually rather than accumulating across calls.
- The sub-run **inherits** the parent's `context` (deps), `cancel_token`
  (one cancel stops the whole tree), and tracer (spans join one trace); its
  token usage folds into the parent's `usage`.
- The sub-run gets its **own mailbox** — deliberately not the parent's,
  since mailbox drains are destructive and an injected message is addressed
  to one conversation.
- A sub-run that exhausts its own budget surfaces as a tool-error result the
  parent can react to — a recoverable delegation failure, not a run-ending
  one ([error semantics](tools.md#error-semantics)).

## Choosing between them

| You want... | Use |
| --- | --- |
| The user to *continue talking* to a specialist | handoff |
| An answer to a bounded subtask, then carry on | agent-as-tool |
| The specialist to see the full conversation | handoff |
| Isolation — the child must not see parent history | agent-as-tool |
| The final answer attributed to the specialist (`result.final_agent`) | handoff |
| Several delegations, possibly in parallel turns | agent-as-tool |

Larger patterns — chaining, routing, parallelization,
orchestrator-workers, evaluator loops — need no framework support: they are
plain Python around `Runner.run`. The
[`examples/workflows/`](../../examples/workflows/) directory implements each
of Anthropic's *Building effective agents* patterns in a page of code.

## Sharp edges

- **Handoff targets need discoverable descriptions.** Two specialists with
  default descriptions look nearly identical to the router; mis-routing is a
  prompt problem before it is a framework problem.
- **`output_type` follows the run, not the agent, when overridden.** A
  `Runner.run(..., output_type=...)` override binds every agent a handoff
  reaches; without it, each agent's own `output_type` applies — a triage →
  specialist chain with different output types changes contract mid-run.
- **Non-ASCII agent names produce digest tool names.** `transfer_to_agent_a1b2c3d4`
  routes fine but reads poorly in logs; set `Handoff(name=...)` /
  `as_tool(name=...)` for readable overrides.
- **Deep as-tool trees multiply cost quietly.** Usage folds upward, so read
  `result.usage` as the *tree total* — budget the root run accordingly.

## See also

- [Tools](tools.md) — both primitives are ordinary tools underneath
- [Sessions & checkpoints](sessions-and-checkpoints.md) — resume across handoffs
- Examples: [`07_handoff.py`](../../examples/07_handoff.py),
  [`08_agent_as_tool.py`](../../examples/08_agent_as_tool.py),
  [`workflows/`](../../examples/workflows/)
