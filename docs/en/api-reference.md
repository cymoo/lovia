# API reference

This page is a compact index of the public surface used most often. Each table
links to the guide that explains behavior, lifecycle, and edge cases.

## Entry points

| API | Result | Use |
| --- | --- | --- |
| `await Runner.run(agent, input, **options)` | `RunResult` | Run to completion in async code |
| `Runner.run_sync(agent, input, **options)` | `RunResult` | Run to completion in a script without an active event loop |
| `Runner.stream(agent, input, **options)` | `RunHandle` | Consume typed events and await the final result |
| `agent.run(...)` / `run_sync(...)` / `stream(...)` | same as above | Instance-method convenience forms |

All three Runner forms accept the same public options:

| Option | Default | Purpose |
| --- | --- | --- |
| `context` | `None` | Application dependencies exposed as `ctx.deps` |
| `output_type` | Agent setting | Override structured output for this Run |
| `extra_instructions` | `None` | Append Run-specific system instructions |
| `max_turns` | `50` | Limit logical model Turns |
| `budget` | `None` | Apply a `RunBudget` |
| `cancel_token` | auto-created | Supply a cooperative `CancelToken` |
| `mailbox` | auto-created | Supply a `Mailbox` for mid-Run steering |
| `retry` | Agent setting | Override Provider retry posture |
| `context_policy` | Agent setting | Override context View shaping |
| `session` / `session_id` | `None` | Load and append conversation history |
| `checkpoint` | `None` | Resume or replay an idempotent Run |
| `tracer` | `None` | Record timed spans |

See [Running agents](running.md) for input forms and lifecycle semantics.

## Agent

`Agent` is configuration, not conversation state. Its primary fields are:

`name`, `instructions`, `model`, `tools`, `plugins`, `handoffs`,
`output_type`, `output_repair`, `settings`, `retry`, `context_policy`,
`workspace`, `hooks`, `approval_handler`, `input_guardrails`,
`output_guardrails`, `default_tool_retries`, `default_tool_timeout`,
`max_tool_output_chars`, and `tool_result_renderer`.

Use `agent.clone(**overrides)` for variants. The field defaults and instruction
forms are documented in [Agents](agents.md#the-fields).

## RunResult and RunHandle

| `RunResult` field | Meaning |
| --- | --- |
| `output` | Final `str` or validated `output_type` instance |
| `entries` | This Run's own Transcript contribution, excluding prior Session history |
| `messages` | Lossy chat-format projection of `entries` |
| `final_agent` | Agent active when the Run completed |
| `usage` | Cumulative input, output, cache, and total Token usage |
| `turns` | Number of logical model Turns |
| `finish_reason` | Final Provider finish reason, if reported |

`RunHandle` is async-iterable and awaitable. Event iteration ends with
`RunCompleted` or `RunFailed`; `await handle.result()` returns the result or
raises the Run failure. `handle.cancel()` requests cooperative cancellation,
and `handle.approvals` exposes the out-of-band approval channel.

## RunContext

The same live `RunContext[T]` reaches Tools, Hooks, guardrails, and dynamic
instruction fragments.

| Field | Meaning |
| --- | --- |
| `deps` / `context` | Object passed as `Runner.run(..., context=...)` |
| `entries` | Live canonical Transcript; treat as read-only |
| `messages` | Fresh chat-format projection of `entries` |
| `agent` | Currently active Agent; changes on Handoff |
| `usage` | Cumulative usage so far |
| `turn` | Current 1-based Turn; `0` before the first Turn |
| `session_id` / `run_id` | Persistence keys, or `None` when unused |
| `budget` | Active `RunBudget`, if any |
| `workspace` | Active Workspace session, if any |
| `cancel_token` | Always-present cooperative cancellation signal |
| `mailbox` | Always-present steering channel |
| `system_prompt` | Fully rendered system prompt for the active Agent |

## Tools and plugins

`@tool` builds a `Tool` from a typed function. Common options are `name`,
`description`, `strict`, `retries`, `timeout`, `parallel`,
`max_output_chars`, `result_renderer`, `needs_approval`, and `policies`.
See [Tools](tools.md).

A `Plugin` has a stable `name` and async `setup()` returning a
`PluginInstance`. An instance may contribute `tools`, `instructions`,
`view_injectors`, `hooks`, `input_guardrails`, `output_guardrails`, and
`aclose`. See [Plugins](plugins.md).

## Exceptions

Every framework exception inherits `LoviaError` and may expose `.hint`.

| Exception | Meaning |
| --- | --- |
| `UserError` | Invalid or missing caller configuration |
| `ProviderError` | Provider request or response failure; may include `vendor`, `model`, `status_code`, `retryable` |
| `ContextOverflowError` | Prompt exceeds the endpoint window after recovery; may include `reported_window` |
| `ToolError` | Structured Tool failure intended for the model or caller |
| `InvalidToolArguments` | Tool arguments failed Schema validation |
| `OutputValidationError` | Final answer could not become `output_type`; includes `raw`, `output_type_name` when available |
| `MaxTurnsExceeded` | Run exhausted `max_turns` without a final answer |
| `BudgetExceeded` | A `RunBudget` limit was exceeded |
| `RunCancelled` | The Run's `CancelToken` was triggered |
| `GuardrailTripped` | An input or output guardrail rejected the value |
| `MCPError` | MCP connection or protocol call failed |

Catch `LoviaError` for the whole framework, or a concrete subclass when the
application has a specific recovery path.

## Common imports

The most common types are re-exported from `lovia`:

```python
from lovia import (
    Agent,
    Runner,
    RunContext,
    RunResult,
    Tool,
    Plugin,
    RunBudget,
    RetryPolicy,
    Compaction,
    tool,
)
```

Integration-specific types remain in focused modules, such as
`lovia.workspace`, `lovia.web`, and `lovia.eval`.
