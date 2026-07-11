# 核心概念

理解五个概念，就能把整个框架串联起来。每个概念都对应一类实践中常见的问题。本页会先说明
它们各自解决什么，再完整梳理一次运行流程。掌握这些概念后，后续文档中的术语便不再赘述。

先用一分钟了解这五个概念：

- **Agent 与 Runner**：`Agent` 是不可变配置；`Runner` 负责执行一次运行。对话状态
  不保存在 Agent 上。
- **轮次（turn）**：一次运行由多个轮次组成。每轮先调用模型，再执行
  它请求的工具。模型在不再请求工具时给出最终答案，循环结束。
- **运行记录与模型视图**：运行记录（transcript）完整保存已经发生的事件；模型视图（view）
  则是某次调用实际发送给模型的内容。对话变长时，只压缩模型视图，不改写运行记录。
- **Session 与 Checkpoint**：Session 保存跨运行的对话历史；Checkpoint 负责单次运行
  内的崩溃恢复。
- **故障处理与资源限制**：基础设施出现问题时如何处理，属于 Agent 配置；单个请求最多
  可以消耗多少资源，则由本次运行的参数决定。

## 主要对象

```python
from lovia import Agent, Runner

agent = Agent(name="writer", instructions="回答要具体。", model="glm-5.2")
result = await Runner.run(agent, "写一段发布说明。")
```

**`Agent`** 是一个声明式数据类，用来定义名称、指令、模型、工具、插件和策略。
它不保存对话状态，因此同一个实例可以同时用于任意数量的运行。
需要调整配置时，可以用 `agent.clone(model="...")` 派生新实例，不会复制任何可变状态。
唯一允许直接修改 Agent 的操作，是用 `@agent.instruction` 注册动态指令片段，详见
[Agent](agents.md)。

**`Runner`** 本身不保存状态。它提供 `run`、`run_sync` 和 `stream` 三个静态方法，
负责根据传入参数启动一次运行。运行期间的可变状态全部由内部循环管理，运行开始时创建，
结束后随即释放。

**`RunResult`** 是一次运行的返回结果，包含 `output`（文本，或经过 `output_type`
校验的对象）、`usage`、`turns`、`finish_reason`、`final_agent`（运行结束时处于活跃状态的
Agent，在发生 Handoff 时尤其有用）以及 `entries`。其中，`entries` 只包含**本次运行新增的**
记录，而不是整段对话历史。

## 一次运行的完整流程

Agent 循环通常会逐渐堆积各种特殊逻辑：审批、重试和持久化各自散落在不同位置，
最终很难判断它们的执行顺序。lovia 将这些操作统一纳入一个阶段明确的循环。
下面按照实际发生的顺序，完整说明一次运行；后续各篇指南会分别展开其中的某个环节。

### 运行前的准备

1. 解析当前活跃的 Agent，包括 Provider、结构化输出配置、工作区 Session、每个插件的
   `setup()`，以及合并后的工具集。工具可能来自 Agent、插件、工作区和 Handoff，最后还会
   加入 `recall_tool_result` 等上下文策略工具。如果你显式定义了同名工具，则优先使用你的定义。
2. 按照“系统提示词 + 既有 Session 历史 + 本次输入”的顺序构建运行记录。系统提示词由
   Agent 指令（包括动态片段和本次运行的 `extra_instructions`）、工作区指令和插件指令组成；
   如果 Provider 不支持原生 JSON Schema，还会追加结构化输出契约。
3. 对构建完成的运行记录执行一次**输入护栏**检查。

### 每一轮的处理步骤

1. 检查 `max_turns`、取消状态和预算等运行限制。
2. 触发 `TurnStarted`，取出 **mailbox** 中排队的消息，并以用户消息的形式追加到运行记录。
   运行过程中插入的新指令会在此时生效。
3. **上下文策略**生成本次模型调用所需的视图；插件的**视图注入器**可以继续添加临时内容，
   例如待办事项提醒。这些临时内容不会持久化。
4. Provider 以流式方式返回模型回复，包括文本增量、推理增量和工具调用增量。如果尚未返回
   任何内容就发生上下文溢出，上下文策略可以再压缩一次视图，并重试本次调用。
5. 将模型回复追加到运行记录；如果配置了 Checkpoint，同时保存当前进度。
6. 如果模型请求调用工具，先按请求顺序逐一完成执行前检查（预算、审批和参数校验），再开始执行。
   允许并发的工具会并行运行，其余工具则依次运行。每个工具完成后立即追加结果并保存 Checkpoint。
7. 如果模型没有请求工具，而是直接给出答案，则解析最终输出。如果结构化输出解析失败，
   可以再执行一轮**修复**，而不是立即结束运行。此行为可以配置。
8. 触发 `TurnEnded`。如果存在待处理的 **Handoff**，则切换当前活跃的 Agent，使用新的
   系统提示词继续处理同一段对话。

### 运行结束

模型给出最终答案后，系统先执行**输出护栏**，再完成 Checkpoint，最后才将本次运行新增的记录
写入 Session。这个顺序固定不变，因此即使发生崩溃，也不会出现同一次运行既被标记为完成、
又仍可恢复的矛盾状态。上述每个事件还会同步分派给[钩子](observability.md)。

流式运行还有一项重要保证：**遍历事件流时，不会因为运行失败而抛出异常。**每个事件流
只会以一个终止事件结束，即 `RunCompleted` 或 `RunFailed`。只有调用
`await handle.result()` 获取结果时，运行错误才会作为异常抛出。

## 运行记录与模型视图

随着对话不断变长，内容终会超出模型的上下文窗口。许多框架通过改写历史记录来解决这个问题，
但这样既难以追溯模型当时实际接收的内容，也可能使恢复后的运行偏离原有路径。

lovia 将完整记录与模型实际接收的内容分开处理：

- **运行记录（transcript）**是权威且只追加的记录，由不同类型的 `TranscriptEntry` 组成，
  包括用户输入、模型文本、推理内容、工具调用和工具结果。Provider 返回的内容会完整保留，
  Session 和 Checkpoint 持久化的也是这份记录。它只会增加，不会被改写。
- **模型视图（view）**是单次模型调用实际接收到的内容。上下文策略（默认为 `Compaction`）
  可以从视图中移出过大的工具结果、清理较早的结果，或汇总早期对话，但不会修改运行记录。
  模型仍可通过 `recall_tool_result` 工具取回从视图中移出的内容。

这样便能明确区分“模型当前看不到某段内容”和“系统没有保存这段内容”。详见
[上下文管理](context.md)。

## Session 与 Checkpoint

这两种持久化存储容易混淆，但用途截然不同：

| | Session | Checkpoint |
| --- | --- | --- |
| 回答的问题 | “这段对话到目前为止说过什么？” | “这次运行走到哪里了？” |
| 键 | `session_id`（你决定：用户 id、线程 id 等） | `run_id`（在一个 checkpointer 内全局唯一） |
| 保存什么 | 每个**已完成**运行的一个 segment | 可能还需要恢复的那次运行 |
| 写入时机 | 运行完成时写一次 | 模型轮次后、每个工具结果后 |
| 生命周期 | 对话的生命周期 | 运行的生命周期（成功后可选删除） |

两者都采用**追加写入**，已经保存的运行不会被改写。任意时刻的完整对话，都由
`session.load()` 返回的历史加上当前运行快照中的记录组成。如果再次提交一个已经完成的
`run_id`，系统会直接重放已保存的结果，不再调用模型。因此，`run_id` 可以作为幂等键；
Worker 崩溃后，也可以安全地重新执行整个任务。详见
[Session 与 Checkpoint](sessions-and-checkpoints.md)。

## 故障处理与资源限制

如果没有清晰的配置边界，可靠性选项很容易散落在各个调用点，导致每次调用都要重复传入大量参数。
lovia 按照以下原则划分配置：

- **故障处理策略**：定义基础设施出现问题时 Agent 如何处理，配置在 `Agent` 上，并由每次运行继承：
  provider `retry`、`default_tool_retries` / `default_tool_timeout`、`context_policy`。
- **资源限制**：规定单个请求最多可以消耗多少资源，通过 `Runner.run` 的参数设置，Agent 上没有对应字段：
  `max_turns`、`budget`、`cancel_token`。

需要特别注意：**初始** Agent 的故障处理策略会贯穿整次运行，即使发生 Handoff 也不会改变。
Handoff 只会更换处理对话的 Agent，不会改变运行流程。如果某个请求需要特殊配置，也可以在调用时覆盖这些策略：
`Runner.run(..., retry=..., context_policy=...)`。见[可靠性](reliability.md)。

## RunContext：访问运行状态

工具、钩子、护栏和动态指令片段都会收到同一个实时 `RunContext`，并通过它访问当前运行状态。
工具只需为某个参数添加相应的**类型标注**即可接收它；参数名称没有限制：

```python
from dataclasses import dataclass

from lovia import RunContext, tool


@dataclass
class Deps:
    db: "Database"


@tool
async def lookup(ctx: RunContext[Deps], user_id: int) -> str:
    """读取用户记录。"""
    return await ctx.deps.db.fetch(user_id)
```

| 字段 | 含义 |
| --- | --- |
| `deps`（别名 `context`） | 你传给 `Runner.run(..., context=...)` 的对象 |
| `entries` | 实时 transcript；请按只读对待 |
| `messages` | 从 `entries` 派生出的 chat 格式视图，每次访问都重新生成 |
| `agent` | 当前活跃 agent（handoff 后会变） |
| `usage` | 到目前为止累计的 token 用量 |
| `turn` | 正在执行的 turn，1-based |
| `session_id` / `run_id` | 本次运行的持久化键；未使用时为 `None` |
| `budget` | 本次运行的 `RunBudget`，供工具自我节流 |
| `workspace` | 当前活跃 agent 的工作区 session，如有 |
| `cancel_token` | 始终存在；工具或 hook 可以请求取消 |
| `mailbox` | 始终存在；推入消息后，模型下一轮可见 |
| `system_prompt` | 本次运行完整渲染后的 system prompt |

## 插件：统一的扩展机制

框架的扩展点很容易变得零散：工具和提示词片段使用不同的注册表，中间件和生命周期回调
又各有一套机制。这样一来，每项可复用能力都必须分别接入多个位置。

lovia 用一个**插件（plugin）**对象统一提供工具、系统提示词、每轮视图注入器、钩子和护栏。
Runner 会在每次运行中激活一次插件（`await plugin.setup()`），并在运行结束后清理；
插件提供的各项能力会合并到前述运行流程的对应阶段。插件不会控制运行流程，中止、重试和
Handoff 仍由运行循环统一管理。

Skills、MCP、待办事项和长期记忆都基于这套机制实现。详见[插件](plugins.md)。

## 错误处理

框架抛出的所有异常都继承自 `LoviaError`。因此，使用 `except LoviaError` 可以只捕获 lovia
自身的错误，而不会意外掩盖业务代码中的缺陷。异常还可以携带可选的 `hint`，在错误消息后给出
下一步处理建议。

| 异常 | 何时抛出 |
| --- | --- |
| `UserError` | 框架配置有误，例如未设置模型或选项不合法；应修改调用代码 |
| `ProviderError` | 模型 API 失败；包含 `vendor`、`status_code`、`retryable` |
| `ContextOverflowError` | 提示词超过上下文窗口且无法通过压缩解决；若端点返回窗口上限，则包含 `reported_window` |
| `ToolError` | 工具以适合结构化呈现的方式失败（通常由你抛出） |
| `InvalidToolArguments` | 工具参数没有通过 schema 校验（会反馈给模型修正） |
| `OutputValidationError` | 最终答案无法解析为 `output_type`，包括修复尝试失败的情况 |
| `MaxTurnsExceeded` | 循环达到 `max_turns` 仍没有最终答案 |
| `BudgetExceeded` | `RunBudget` 的某项限制在运行中触发 |
| `RunCancelled` | `CancelToken` 被触发 |
| `GuardrailTripped` | 输入/输出护栏拒绝了某个值 |
| `MCPError` | MCP 服务器连接或调用失败 |

还需注意两点。第一，工具抛出普通异常时，运行**不会**立即结束；该错误会作为工具结果返回给模型，
让模型有机会调整策略，详见[工具](tools.md)。第二，在流式模式下，这些异常通过
`handle.result()` 抛出，遍历事件流本身不会抛出运行异常。

## 可以依赖的设计约束

“简洁、轻量、可扩展、通用”不仅是设计理念，也落实为以下可以依赖的约束：

- **Agent 只保存配置。** `Agent` 不包含对话状态，可以安全共享，也可以轻松派生不同配置。
- **运行记录永不重写。** 压缩只改变模型视图；Session 和 Checkpoint 只追加；
  完成后的运行不可变。
- **插件提供能力，循环控制流程。** 插件不能自行重试、中止运行或更改路由。
- **所有关联都通过 ID 建立，而不依赖位置。** 工具事件通过 `call.id` 配对；Segment 和
  Snapshot 通过 `run_id` 配对。即使并发执行，关键关系也不会被打乱。
- **Provider 采用协议接口。** 两个内置适配器通过 `httpx` 调用 OpenAI 和 Anthropic；
  接入新的 Provider 只需实现 `Protocol`，无须继承基类。
- **核心保持精简。** 导入 `lovia` 时不会同时加载 Web 技术栈或工作区机制；这些层依赖核心，
  核心不反向依赖它们。

## 延伸阅读

- [快速上手](quickstart.md)：运行一个最小 Agent
- [运行 Agent](running.md)：`Runner` 的完整用法
- [架构说明](../architecture.md)：面向贡献者的详细版本，包含模块名称和修改 lovia 本身时
  需要遵守的不变量
