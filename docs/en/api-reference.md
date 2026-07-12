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

`Agent` is reusable configuration, not conversation state.

| Field | Default | Description |
| --- | --- | --- |
| `name` | required | Human-readable identity; also derives Handoff Tool names |
| `instructions` | `""` | Static system text or sync/async callable receiving `RunContext` |
| `model` | `None` | `"vendor:model"`, bare OpenAI-compatible model name, or `Provider`; required before running |
| `tools` | `[]` | Tools directly available to the Agent |
| `output_type` | `str` | Final output type: Pydantic model, dataclass, TypedDict, or builtin |
| `output_repair` | `True` | Repair invalid structured output once, disable, or supply a custom strategy |
| `handoffs` | `[]` | Agents or `Handoff` definitions the model may transfer to |
| `settings` | `ModelSettings()` | Sampling and model request settings |
| `retry` | `RetryPolicy()` | Provider retry posture; `None` disables it |
| `context_policy` | `Compaction()` | Per-call Transcript View shaping |
| `workspace` | `None` | Optional file and Shell capability provider |
| `plugins` | `[]` | Plugins activated once per Run and per Handoff target |
| `hooks` | `None` | `AgentHooks` event subscribers |
| `approval_handler` | `None` | Programmatic allow/deny/ask policy for gated Tools |
| `input_guardrails` | `[]` | Checks before the first model call |
| `output_guardrails` | `[]` | Checks before returning the final output |
| `default_tool_retries` | `0` | Retry count for Tools whose `retries` is `None` |
| `default_tool_timeout` | `None` | Per-attempt seconds for Tools whose `timeout` is `None` |
| `max_tool_output_chars` | `200_000` | Agent-wide cap on rendered Tool results; `None` stores in full |
| `tool_result_renderer` | `None` | Agent-wide successful Tool-result renderer |

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

| `RunHandle` API | Returns | Description |
| --- | --- | --- |
| `async for event in handle` | `Event` stream | Single-use stream ending in `RunCompleted` or `RunFailed`; Run failures do not raise during iteration |
| `await handle` | `RunResult` | Await the final result or raise the stored Run failure |
| `await handle.result()` | `RunResult` | Same result contract; drives the stream when it has not been consumed |
| `handle.cancel(reason=None)` | `None` | Request cooperative cancellation at the next safe point |
| `handle.approvals` | `ApprovalChannel` | Resolve pending Tool approvals by call ID |

A Handle may only be iterated once. If iteration is abandoned before a
terminal event, `result()` raises instead of returning a partial result.

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

## Tool

`@tool` derives the first four fields from a typed function. Construct `Tool`
directly for factories and generated schemas.

| Field / decorator option | Default | Description |
| --- | --- | --- |
| `name` | function name | Unique name exposed to the model |
| `description` | docstring | Guidance exposed in the Tool schema |
| `parameters` | derived | JSON Schema for model-supplied arguments |
| `invoke` | wrapped function | Async callable receiving raw arguments and `RunContext` |
| `strict` | `False` (`@tool` only) | Require complete function annotations and strict schema generation when enabled |
| `needs_approval` | `False` | Boolean or predicate that gates execution on approval |
| `retries` | `None` | Retries after the first attempt; `None` inherits Agent default |
| `timeout` | `None` | Per-attempt seconds; `None` inherits Agent default |
| `parallel` | `True` | Whether the call may overlap other calls in the same Turn |
| `max_output_chars` | `None` | Result cap; `None` inherits the Agent cap |
| `result_renderer` | `None` | Convert a successful raw result into model-visible text |
| `policies` | `()` | Per-attempt wrappers for caching, auth, redaction, or custom behavior |

See [Tools](tools.md) for schema derivation, execution, approval, and errors.

## Plugin and PluginInstance

| `Plugin` member | Required | Description |
| --- | --- | --- |
| `name` | yes | Stable identity, unique within one Agent |
| `async setup()` | yes | Create fresh per-Run contributions and return `PluginInstance` |

| `PluginInstance` field | Default | Description |
| --- | --- | --- |
| `tools` | `[]` | Tools merged into the Agent namespace |
| `view_injectors` | `[]` | Per-turn callables adding transient Transcript entries to the model View |
| `instructions` | `None` | Static text appended to the system prompt |
| `hooks` | `None` | Event handlers dispatched with Agent Hooks |
| `input_guardrails` | `[]` | Checks merged at the input checkpoint |
| `output_guardrails` | `[]` | Checks merged at the output checkpoint |
| `aclose` | no-op coroutine | Best-effort asynchronous resource cleanup |

See [Plugins](plugins.md) for lifecycle and state-scoping rules.

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
