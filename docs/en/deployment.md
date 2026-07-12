# Deployment

Production readiness is mostly about the boundaries around an Agent: who may
call it, what it may touch, how much it may spend, and what survives a crash.
Use this checklist before exposing a lovia application beyond one trusted
user.

## Deployment checklist

| Boundary | Production choice |
| --- | --- |
| Authentication | Put the Web/API server behind your authentication gateway; lovia does not provide auth |
| Network | Keep the default loopback bind unless a protected proxy is in front |
| Workspace | Start with `readonly` or disabled; a writable local Workspace is host-level code execution |
| Secrets | Pass only required environment variables; Workspace Shell uses a minimal environment unless `inherit_env=True` |
| Limits | Set `max_turns`, a `RunBudget`, provider timeouts, Tool timeouts, and output caps |
| Risky actions | Require approval for writes, external side effects, scheduling, and privileged calls |
| Persistence | Use a Session for conversations and a Checkpointer for in-flight recovery; back up their stores |
| Observability | Record terminal events, failures, latency, token usage, and approval decisions |
| Concurrency | Keep the bundled Web server at one worker; supervised runs and approvals are process-local |
| TLS and proxies | Configure CA bundles and proxy trust explicitly; do not disable verification in production |

## A conservative starting point

```python
from lovia import Agent, RunBudget
from lovia.workspace import Workspace

agent = Agent(
    name="service-agent",
    model="<model>",
    workspace=Workspace.local("./data", mode="readonly"),
    default_tool_timeout=30,
    max_tool_output_chars=50_000,
)

budget = RunBudget(max_total_tokens=1_000_000, max_tool_calls=50)
```

Pass the budget and a persistence configuration at the application boundary:

```python
result = await agent.run(
    "Analyze the latest report.",
    max_turns=12,
    budget=budget,
    session=session,
    session_id=user_conversation_id,
    checkpoint=checkpoint,
)
```

The exact limits are workload-specific; what matters is that they are explicit
before traffic arrives.

## Serving safely

!!! danger "The bundled server has no authentication"

    `lovia web` and `create_app()` trust every request. Bind to loopback, or
    deploy behind an authenticated reverse proxy with rate limits. A public
    server combined with a writable Workspace is remote code execution as the
    server user.

The bundled server is designed for one process. SQLite data is durable, but
live Runs, approvals, SSE subscribers, and scheduling coordination are held in
that process. Use `workers=1`; scale by running isolated application instances
with deliberate routing and storage ownership.

## Failure and recovery

- Retry transient Provider failures inside a Run with `RetryPolicy`.
- Use Checkpoints when a worker may restart mid-Run.
- Use Sessions for completed conversation history; do not reuse a completed
  `run_id` for the next user message.
- Treat Tool side effects as at-least-once under crash recovery unless the Tool
  implements idempotency with `ctx.run_id` and its arguments.
- Back up SQLite or custom stores and test restoration before relying on them.

## See also

- [Provider retries](retries.md), [Budgets](budgets.md), and [Cancellation](cancellation.md)
- [Sessions & checkpoints](sessions-and-checkpoints.md) — durability and idempotency
- [Workspace](workspace.md) — ACLs and the Shell security boundary
- [Web server](web-server.md) — server lifecycle and configuration
- [Observability](observability.md) — Hooks, tracing, logging, and usage
