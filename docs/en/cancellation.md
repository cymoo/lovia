# Cancellation & steering

Two channels let an application control a live run: a `CancelToken` stops work,
while a `Mailbox` adds new user guidance at the next turn boundary.

## Cancellation

```python
from lovia import Agent, CancelToken, Runner

agent = Agent(name="analyst", model="<model>")
token = CancelToken()
handle = Runner.stream(agent, "Run a long analysis.", cancel_token=token)

# From an event handler, request handler, or another coroutine:
token.cancel("user clicked stop")
# Equivalent when you have the handle: handle.cancel("user clicked stop")
```

The run raises `RunCancelled` at the next safe point and its stream ends with
`RunFailed`. A mid-batch cancellation also cancels asynchronous sibling tool
calls that are still running.

The token is always available as `ctx.cancel_token`, even when the caller did
not supply one. Hooks and tools can therefore stop their own run. Agent-as-tool
sub-runs inherit the parent token, so one cancellation stops the whole tree.

Cancellation cannot interrupt a synchronous tool's worker thread or retract a
provider request already sent. The thread may finish and its side effects may
still occur after the run ends.

## Steering a live run

Use a `Mailbox` when the user refines the task while the Agent is working:

```python
from lovia import Agent, Mailbox, Runner

agent = Agent(name="analyst", model="<model>")
mailbox = Mailbox()
handle = Runner.stream(agent, "Analyze these logs.", mailbox=mailbox)

mailbox.push("Focus on the 5xx spike around 14:00.")
result = await handle
```

The runner drains queued messages at the start of each turn and persists each
one as a normal user message. A push never interrupts the current model or tool
phase.

| Operation | Effect |
| --- | --- |
| `token = mailbox.push(content)` | Queue content for the next drain |
| `mailbox.remove(token)` | Withdraw content that has not been drained |
| `ctx.mailbox.push(content)` | Steer from a Tool or Hook |

A `TurnStarted` Hook runs immediately before that turn's drain, so a push from
the Hook lands in the same turn. Other pushes land in the next turn. Each
drained item emits [`UserMessageInjected`](streaming.md#model-output).

Messages left in a caller-supplied mailbox remain available for a later run.
Messages left in the runner-created default mailbox are unreachable. An
Agent-as-tool sub-run deliberately gets its own mailbox.

## See also

- [Budgets & limits](budgets.md) — stop automatically at resource limits
- [Streaming](streaming.md) — observe cancellation and injected messages
- Examples: [`14_reliability.py`](../../examples/14_reliability.py),
  [`16_steering.py`](../../examples/16_steering.py)
