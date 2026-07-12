# API 参考

本页汇总最常用的公开接口，并链接到解释行为、生命周期和边界条件的详细指南。

## 运行入口

| API | 返回值 | 用途 |
| --- | --- | --- |
| `await Runner.run(agent, input, **options)` | `RunResult` | 在异步代码中运行至完成 |
| `Runner.run_sync(agent, input, **options)` | `RunResult` | 在没有活动事件循环的脚本中运行至完成 |
| `Runner.stream(agent, input, **options)` | `RunHandle` | 消费类型化事件并等待最终结果 |
| `agent.run(...)` / `run_sync(...)` / `stream(...)` | 同上 | 对应的实例方法简写 |

三种 Runner 入口接受相同的公开选项：

| 选项 | 默认值 | 用途 |
| --- | --- | --- |
| `context` | `None` | 作为 `ctx.deps` 暴露的应用依赖 |
| `output_type` | Agent 配置 | 覆盖本次 Run 的结构化输出类型 |
| `extra_instructions` | `None` | 追加 Run 级系统指令 |
| `max_turns` | `50` | 限制逻辑模型 Turn 数量 |
| `budget` | `None` | 应用 `RunBudget` |
| `cancel_token` | 自动创建 | 提供协作式 `CancelToken` |
| `mailbox` | 自动创建 | 提供用于 Run 中途引导的 `Mailbox` |
| `retry` | Agent 配置 | 覆盖 Provider 重试策略 |
| `context_policy` | Agent 配置 | 覆盖上下文 View 生成策略 |
| `session` / `session_id` | `None` | 加载并追加对话历史 |
| `checkpoint` | `None` | 恢复或重放具有幂等性的 Run |
| `tracer` | `None` | 记录耗时 Span |

输入形式和生命周期语义详见[运行 Agent](running.md)。

## Agent

`Agent` 保存配置，而不是对话状态。主要字段包括：

`name`、`instructions`、`model`、`tools`、`plugins`、`handoffs`、
`output_type`、`output_repair`、`settings`、`retry`、`context_policy`、
`workspace`、`hooks`、`approval_handler`、`input_guardrails`、
`output_guardrails`、`default_tool_retries`、`default_tool_timeout`、
`max_tool_output_chars` 和 `tool_result_renderer`。

派生变体时使用 `agent.clone(**overrides)`。字段默认值和 Instructions 形式详见
[Agent](agents.md#字段)。

## RunResult 与 RunHandle

| `RunResult` 字段 | 含义 |
| --- | --- |
| `output` | 最终字符串，或经过校验的 `output_type` 实例 |
| `entries` | 本次 Run 新增的 Transcript，不包含既有 Session 历史 |
| `messages` | 从 `entries` 派生的有损聊天格式视图 |
| `final_agent` | Run 完成时处于活跃状态的 Agent |
| `usage` | 累计输入、输出、缓存和总 Token 用量 |
| `turns` | 逻辑模型 Turn 数量 |
| `finish_reason` | Provider 返回的最终停止原因，如有 |

`RunHandle` 同时支持异步迭代和等待。事件流以 `RunCompleted` 或 `RunFailed` 结束；
`await handle.result()` 返回结果或抛出 Run 异常。`handle.cancel()` 请求协作式取消，
`handle.approvals` 则提供带外审批通道。

## RunContext

同一个实时 `RunContext[T]` 会传给 Tool、Hook、护栏和动态指令片段。

| 字段 | 含义 |
| --- | --- |
| `deps` / `context` | 通过 `Runner.run(..., context=...)` 传入的对象 |
| `entries` | 实时权威 Transcript；应按只读数据处理 |
| `messages` | 每次访问时重新生成的聊天格式视图 |
| `agent` | 当前活跃 Agent；Handoff 后会变化 |
| `usage` | 当前累计用量 |
| `turn` | 当前 Turn，从 1 开始；第一轮前为 `0` |
| `session_id` / `run_id` | 持久化键；未使用时为 `None` |
| `budget` | 当前 `RunBudget`，如有 |
| `workspace` | 当前 Workspace Session，如有 |
| `cancel_token` | 始终存在的协作式取消信号 |
| `mailbox` | 始终存在的引导通道 |
| `system_prompt` | 当前 Agent 完整渲染后的系统提示词 |

## Tool 与插件

`@tool` 根据带类型信息的函数构建 `Tool`。常用选项包括 `name`、`description`、
`strict`、`retries`、`timeout`、`parallel`、`max_output_chars`、
`result_renderer`、`needs_approval` 和 `policies`。详见[工具](tools.md)。

`Plugin` 具有稳定的 `name`，以及返回 `PluginInstance` 的异步 `setup()`。实例可以提供
`tools`、`instructions`、`view_injectors`、`hooks`、`input_guardrails`、
`output_guardrails` 和 `aclose`。详见[插件](plugins.md)。

## 异常

所有框架异常都继承自 `LoviaError`，并可提供 `.hint`。

| 异常 | 含义 |
| --- | --- |
| `UserError` | 调用方配置缺失或无效 |
| `ProviderError` | Provider 请求或响应失败；可能包含 `vendor`、`model`、`status_code`、`retryable` |
| `ContextOverflowError` | 恢复后提示词仍超出端点窗口；可能包含 `reported_window` |
| `ToolError` | 有意提供给模型或调用方的结构化 Tool 失败 |
| `InvalidToolArguments` | Tool 参数未通过 Schema 校验 |
| `OutputValidationError` | 最终答案无法转换为 `output_type`；可能包含 `raw`、`output_type_name` |
| `MaxTurnsExceeded` | Run 用完 `max_turns` 仍未得到最终答案 |
| `BudgetExceeded` | 超出某项 `RunBudget` 限制 |
| `RunCancelled` | Run 的 `CancelToken` 被触发 |
| `GuardrailTripped` | 输入或输出护栏拒绝了值 |
| `MCPError` | MCP 连接或协议调用失败 |

如果要捕获整个框架的错误，可以使用 `LoviaError`；如果应用有明确恢复路径，应捕获具体子类。

## 常用导入

最常用的类型都可以从 `lovia` 顶层导入：

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

集成相关类型保留在专门模块中，例如 `lovia.workspace`、`lovia.web` 和 `lovia.eval`。
