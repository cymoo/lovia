# Human in the loop

Two directions of human involvement, two mechanisms. **Approval**: the
*runner* pauses a gated tool call until someone (or something) decides.
**`ask_human`**: the *model* asks your operator a question and waits for
the answer. Both fail safe — an unanswered approval denies; a closed
channel errors the tool call.

## Tool approval

Gate a tool with `needs_approval` — a bool, or a predicate over the parsed
arguments:

```python
from lovia import tool


@tool(needs_approval=True)
async def refund(order_id: str, amount_cents: int) -> str:
    """Issue a refund."""
    return "refunded"


@tool(needs_approval=lambda args, ctx: args["amount_cents"] > 5_000)
async def discount(order_id: str, amount_cents: int) -> str:
    """Apply a discount — small ones auto-approved."""
    return "applied"
```

When a gated call comes up, the runner emits
[`ApprovalRequired`](streaming.md#tools-and-approval) and waits. Three
resolution paths, consulted in order:

**1. The streaming consumer** — resolve on the event, right in the loop:

```python
from lovia import Runner, events

handle = Runner.stream(agent, "Refund order A123.")

async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()          # or ev.reject()
```

**2. The agent's `approval_handler`** — server-side policy, consulted when
the consumer didn't decide:

```python
agent = Agent(
    ...,
    approval_handler=lambda call, ctx: "ask" if call.name == "refund" else "allow",
)
```

It returns `True`/`"allow"`, `False`/`"deny"`, or `"ask"` (defer back to
the consumer/channel). Sync or async. A raising handler counts as deny.

**3. Default: deny.** If nobody decides by the time the turn needs the
answer, the call is rejected — a run can never hang on a forgotten dialog.
The model sees `"Tool {name} was not approved."` and adapts.

### Out-of-band: the approval channel

When the decider isn't the stream consumer — a web endpoint, a Slack bot,
another task — resolve by call id via the handle's channel:

```python
handle = Runner.stream(agent, "Do the maintenance.")
# ... elsewhere, given a call id from the ApprovalRequired event:
handle.approvals.approve(call_id)
handle.approvals.reject(call_id)
handle.approvals.release(decision=False)   # sweep: resolve everything pending
```

The bundled [web server](http-api.md) is exactly this pattern:
`ApprovalRequired` goes out over SSE, `POST /api/chat/approve` calls the
channel.

### Semantics that matter

- **Approval is part of preflight, which runs serially in request order.**
  While one call waits for approval, *already-approved* parallel calls of
  the turn keep executing; calls requested after the gated one wait their
  turn. Approval prompts therefore arrive one at a time, in order.
- **A raising `needs_approval` predicate fails closed** — the call is
  rejected (with a `ToolCallFailed` carrying the exception for observers),
  never run unvetted.
- **Non-streaming callers** (`Runner.run`) can't see events — give the
  agent an `approval_handler`, or gated tools are silently denied (the
  fail-closed default doing its job).
- **[Workspace](workspace.md) `ask` decisions ride the same channel** —
  one approval UI covers your tools, MCP servers, file writes, and shell
  commands.

## Ask a human

The inverse direction — the model needs information only a person has:

```python
from lovia import Agent, Runner
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()

agent = Agent(
    name="assistant",
    model="glm-5.2",
    tools=[ask_human(channel)],
)
```

The model calls `ask_human(question)`; the call blocks until your operator
side answers. The idiomatic consumer is one loop:

```python
async for q in channel.questions():        # ends when channel.close() is called
    channel.answer(q.id, await get_reply_somehow(q.question))
```

The channel API:

| Method | Effect |
| --- | --- |
| `questions()` | async-iterate questions as the model asks them (single consumer; pre-iteration questions are queued) |
| `pending` | poll-style snapshot of unanswered questions |
| `answer(id, text)` | resolve — the tool call returns `text` |
| `cancel(id, reason=...)` | fail that one call with a `ToolError` the model sees |
| `close(reason=...)` | cancel everything outstanding, end `questions()`, fail future asks — idempotent |

Cancellation and closure surface to the model as tool-error results, so it
can proceed without the answer rather than crashing the run. Combine with a
per-tool timeout (`ask_human` is built by a factory — wrap or rebuild it
with `@tool(timeout=...)` semantics via `dataclasses.replace`) when
operators may simply be gone.

Approval asks "may I do this?" with a yes/no; `ask_human` asks "what should
I know?" with free text. If you find yourself encoding data into approvals,
you want `ask_human`; if an `ask_human` reply is always yes/no, you want an
approval gate.

## Sharp edges

- **Resolution must come from the event-loop thread.** Both channels
  resolve `asyncio` futures — from another thread, hop over first:
  `loop.call_soon_threadsafe(channel.answer, qid, text)`.
- **`ev.approve()` after the turn moved on is a silent no-op** — the
  call was already denied by the fail-closed default. Decide before your
  iteration yields control back, or use the out-of-band channel.
- **Approval decisions aren't persisted.** On a
  [resume](sessions-and-checkpoints.md), a pending gated call is
  re-preflighted and asks again.

## See also

- [Streaming](streaming.md) — the `ApprovalRequired` event contract
- [Workspace](workspace.md) — the `ask` tier of the file/shell ACL
- [HTTP API](http-api.md) — approval over SSE + POST
- Examples: [`12_approval.py`](../../examples/12_approval.py),
  [`tools/04_human.py`](../../examples/tools/04_human.py)
