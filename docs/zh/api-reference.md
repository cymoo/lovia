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

`Agent` 保存可复用配置，而不是对话状态。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `name` | 必填 | 人类可读的标识，也用于生成 Handoff Tool 名称 |
| `instructions` | `""` | 静态系统文本，或接收 `RunContext` 的同步/异步 callable |
| `model` | `None` | `"vendor:model"`、无前缀 OpenAI-compatible 模型名或 `Provider`；运行前必须设置 |
| `tools` | `[]` | Agent 可直接使用的 Tool |
| `output_type` | `str` | 最终输出类型：Pydantic Model、dataclass、TypedDict 或内置类型 |
| `output_repair` | `True` | 修复一次无效结构化输出、关闭修复或传入自定义策略 |
| `handoffs` | `[]` | 模型可以转移到的 Agent 或 `Handoff` 定义 |
| `settings` | `ModelSettings()` | 采样和模型请求设置 |
| `retry` | `RetryPolicy()` | Provider 重试姿态；`None` 表示关闭 |
| `context_policy` | `Compaction()` | 每次调用的 Transcript View 生成方式 |
| `workspace` | `None` | 可选的文件与 Shell 能力提供方 |
| `plugins` | `[]` | 每次 Run、每个 Handoff 目标激活一次的 Plugin |
| `hooks` | `None` | `AgentHooks` 事件订阅器 |
| `approval_handler` | `None` | 带门禁 Tool 的程序化 allow/deny/ask 策略 |
| `input_guardrails` | `[]` | 第一次模型调用前执行的检查 |
| `output_guardrails` | `[]` | 返回最终输出前执行的检查 |
| `default_tool_retries` | `0` | `retries` 为 `None` 的 Tool 所使用的重试次数 |
| `default_tool_timeout` | `None` | `timeout` 为 `None` 的 Tool 所使用的单次超时秒数 |
| `max_tool_output_chars` | `200_000` | Agent 级 Tool 渲染结果上限；`None` 表示完整保存 |
| `tool_result_renderer` | `None` | Agent 级成功 Tool 结果渲染器 |

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

| `RunHandle` API | 返回值 | 说明 |
| --- | --- | --- |
| `async for event in handle` | `Event` 流 | 只能消费一次，以 `RunCompleted` 或 `RunFailed` 结束；Run 失败不会在迭代中抛出 |
| `await handle` | `RunResult` | 等待最终结果，或抛出保存的 Run 异常 |
| `await handle.result()` | `RunResult` | 同一结果契约；尚未消费时会主动驱动事件流 |
| `handle.cancel(reason=None)` | `None` | 请求在下一个安全点协作式取消 |
| `handle.approvals` | `ApprovalChannel` | 按 Call ID 解决等待中的 Tool 审批 |

Handle 只能迭代一次。如果在终止事件前放弃迭代，`result()` 会抛出异常，而不会返回部分结果。

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

## Tool

`@tool` 会根据带类型信息的函数推导前四个字段。Tool 工厂或动态 Schema 可直接构造 `Tool`。

| 字段 / 装饰器选项 | 默认值 | 说明 |
| --- | --- | --- |
| `name` | 函数名 | 暴露给模型的唯一名称 |
| `description` | 文档字符串 | Tool Schema 中的用途说明 |
| `parameters` | 自动推导 | 模型提供参数的 JSON Schema |
| `invoke` | 包装后的函数 | 接收原始参数和 `RunContext` 的异步 callable |
| `strict` | `False`（仅 `@tool`） | 启用时要求完整函数注解并生成严格 Schema |
| `needs_approval` | `False` | 在执行前要求审批的布尔值或谓词 |
| `retries` | `None` | 第一次后的重试次数；`None` 继承 Agent 默认值 |
| `timeout` | `None` | 单次尝试秒数；`None` 继承 Agent 默认值 |
| `parallel` | `True` | 是否允许与同一 Turn 的其他调用重叠执行 |
| `max_output_chars` | `None` | 结果上限；`None` 继承 Agent 上限 |
| `result_renderer` | `None` | 把成功的原始结果转换为模型可见文本 |
| `policies` | `()` | 用于缓存、鉴权、脱敏或自定义行为的单次尝试包装器 |

Schema 推导、执行、审批和错误语义见[工具](tools.md)。

## Plugin 与 PluginInstance

| `Plugin` 成员 | 是否必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 稳定标识；在同一 Agent 内必须唯一 |
| `async setup()` | 是 | 创建每次 Run 独享的贡献并返回 `PluginInstance` |

| `PluginInstance` 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `tools` | `[]` | 合并进 Agent 命名空间的 Tool |
| `view_injectors` | `[]` | 每 Turn 向模型 View 添加临时 Transcript Entry 的 callable |
| `instructions` | `None` | 追加到系统提示词的静态文本 |
| `hooks` | `None` | 与 Agent Hook 一起分发的事件处理器 |
| `input_guardrails` | `[]` | 合并到输入检查点的检查 |
| `output_guardrails` | `[]` | 合并到输出检查点的检查 |
| `aclose` | no-op 协程 | 尽力执行的异步资源清理 |

生命周期与状态作用域规则见[插件](plugins.md)。

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
