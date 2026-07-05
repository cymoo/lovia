# 核心概念

整个框架可以用五个概念撑起来。每个概念都对应一个真实会出问题的地方；本页会先
讲它解决什么问题，再把一次运行从头到尾走一遍，这样后面的文档就可以直接使用这些
词。

一分钟版本：

- **Agent vs Runner**：`Agent` 是不可变配置；`Runner` 执行一次运行。对话状态
  不存在 agent 上。
- **轮次（turn）**：一次运行是由多个 turn 组成的循环。每轮先调用模型，再执行
  它请求的工具。模型在不再请求工具时给出最终答案，循环结束。
- **Transcript vs view**：transcript 是追加式记录，说明发生过什么；view 是某次
  模型调用能看到的内容。长对话之所以能活下去，是因为只缩小 view。
- **Session vs checkpoint**：session 是跨运行的对话记忆；checkpoint 是单次运行
  内的崩溃恢复。
- **姿态 vs 限制**：基础设施出问题时 agent 怎么应对，是 agent 配置；某个请求最多
  花多少，是单次运行参数。

## 角色表

```python
from lovia import Agent, Runner

agent = Agent(name="writer", instructions="回答要具体。", model="openai:gpt-5.5")
result = await Runner.run(agent, "写一段发布说明。")
```

**`Agent`** 是一个声明式 dataclass：名称、instructions、模型、工具、插件和策略。
它不保存对话状态，所以一个实例可以服务任意数量的并发运行；
`agent.clone(model="...")` 可以派生一个变体，而不会复制任何可变状态。唯一允许的
原地变更，是用 `@agent.instruction` 注册动态 instruction 片段，见
[Agent](agents.md)。

**`Runner`** 是无状态的。它只有三个静态方法：`run`、`run_sync`、`stream`，
负责把参数变成一次运行。一次运行的所有可变状态都存在运行循环内部，开始时创建，
结束时消失。

**`RunResult`** 是你拿回来的结果：`output`（文本，或你声明的 `output_type`
校验后的对象）、`usage`、`turns`、`finish_reason`、`final_agent`（运行结束时的
活跃 agent，handoff 后有用），以及 `entries`，也就是**本次运行自己**贡献的
transcript 片段，而不是整段对话。

## 一次运行，逐轮看

这个设计要解决的问题是：agent 循环很容易长出一堆特例，审批在这里，重试在那里，
持久化又在另一个角落，最后没人说得清执行顺序。lovia 的做法是一条阶段固定的循环。
下面是真实顺序；每篇指南都挂在其中某一步上。

**每次运行只做一次的准备：**

1. 解析活跃 agent：provider、结构化输出、工作区 session、插件 `setup()`
   （每个插件一次）和合并后的工具集。工具来源包括 agent 工具、插件工具、工作区
   工具、handoff 工具，最后是上下文策略工具，如 `recall_tool_result`
   （最后添加；如果你显式定义了同名工具，则以你的工具为准）。
2. 构建 transcript：`[system prompt] + 之前的 session 历史 + 本次输入`。
   system prompt 会拼接 agent 的 instructions（含动态片段和每次运行的
   `extra_instructions`）、工作区 instructions、插件 instructions，以及在
   provider 不支持原生 JSON schema 时追加的结构化输出契约。
3. 对构建好的 transcript 运行一次**输入护栏**。

**然后进入循环。每次迭代是一轮 turn：**

1. 检查限制：`max_turns`、取消、预算。
2. 触发 `TurnStarted`；队列里的 **mailbox** 消息被取出，作为用户消息追加进
   transcript。这就是运行中追加指令生效的地方。
3. **上下文策略**渲染本次模型调用的 transcript view；插件的**视图注入器**
   追加临时条目（todo 提醒之类，不持久化）。
4. provider 流式返回模型回复：文本 delta、reasoning delta、工具调用 delta。
   如果还没流出内容就发生上下文溢出，策略有一次机会缩小 view 并重试调用。
5. 回复对应的 entries 追加进 transcript；如果配置了 checkpoint，则保存。
6. 如果模型请求了工具：按请求顺序逐个做 **preflight**（预算、审批、参数校验），
   然后执行。允许并发的工具并发执行；不允许并发的工具串行执行。结果完成即追加，
   每个结果都会 checkpoint。
7. 如果模型没有请求工具而是给出答案：解析最终输出。结构化输出解析失败时，会开启
   一轮**修复**，而不是立刻失败（可配置）。
8. 触发 `TurnEnded`。如果有待处理的 **handoff**，切换活跃 agent（新的 system
   prompt，同一段对话主体），然后继续循环。

**完成时：**运行**输出护栏**，finalize checkpoint，然后才把本次运行的片段追加进
session。顺序固定为这样，所以崩溃时不会出现“既已经持久化为完成、又仍然可恢复”的
状态。上面的每个事件都会同时派发给 [hooks](observability.md)。

流式还有一个值得记住的保证：**迭代运行事件流不会因为运行错误而抛异常。**每个流都
以且仅以一个终止事件结束：`RunCompleted` 或 `RunFailed`。真正把错误变成异常的是
`await handle.result()`。

## Transcript vs view

问题是：对话会超过上下文窗口，而很多框架会通过“改写历史”来解决。这样一来，你就
很难审计模型到底看到了什么，恢复运行也容易产生分叉。

lovia 把两个角色拆开：

- **transcript** 是权威、追加式记录：由类型化的 `TranscriptEntry` 组成
  （输入、assistant 文本、reasoning、工具调用、工具结果），保留 provider 发出的
  全部内容。session 和 checkpoint 持久化的是 transcript。它只会增长。
- **view** 是某一次模型调用接收到的内容。上下文策略（默认是 `Compaction`）可以
  在 view 里挪走巨大的工具结果、清理旧结果，或总结很早的历史。注意：只改 view，
  不改 transcript。`recall_tool_result` 工具可以让模型取回 view 里被移走的东西。

于是，“模型忘了”和“记录丢了”变成两个不同问题，也会有不同答案。细节见
[上下文管理](context.md)。

## Session vs checkpoint

这两个持久化存储很容易混在一起，但它们回答的问题不同：

| | Session | Checkpoint |
| --- | --- | --- |
| 回答的问题 | “这段对话到目前为止说过什么？” | “这次运行进行到哪里了？” |
| 键 | `session_id`（你决定：用户 id、线程 id 等） | `run_id`（在一个 checkpointer 内全局唯一） |
| 保存什么 | 每个**已完成**运行的一个 segment | 那个可能还需要恢复的运行 |
| 写入时机 | 运行完成时写一次 | 模型 turn 后、每个工具结果后 |
| 生命周期 | 对话的生命周期 | 运行的生命周期（成功后可选删除） |

两者都是**追加式**的：已经保存的运行不会被重写。任意时刻的完整对话是
`session.load()` 加上正在运行的 snapshot entries。重新提交一个已经完成的
`run_id` 会直接重放保存的结果，不再调用模型；这就是为什么 `run_id` 能作为幂等键，
也让崩溃的 worker 可以简单地重试整个 job。见
[Session 与 Checkpoint](sessions-and-checkpoints.md)。

## 姿态 vs 限制

问题是：可靠性开关很容易散落到每个调用点，最后每次调用都要传十几个参数。lovia 的
放置规则是：

- **姿态**：基础设施出问题时 agent 如何应对，放在 `Agent` 上，并被每次运行继承：
  provider `retry`、`model=[...]` fallback 链、`default_tool_retries` /
  `default_tool_timeout`、`context_policy`。
- **限制**：某个请求最多能花多少，是 `Runner.run` 的参数，没有 agent 侧对应项：
  `max_turns`、`budget`、`cancel_token`。

一个重要结果：**初始** agent 的姿态贯穿整个运行，包括 handoff 之后。转交只改变谁在
说话，不改变运行的骨架。当某个请求确实特殊时，也可以按调用覆盖姿态：
`Runner.run(..., retry=..., context_policy=...)`。见[可靠性](reliability.md)。

## RunContext：唯一的运行句柄

工具、hooks、护栏和动态 instruction 片段都会收到同一个实时 `RunContext`。工具通过
给第一个参数做**类型标注**来选择接收它；参数名不重要，类型标注才重要：

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

## 插件：唯一扩展轴

问题是：框架很容易长出一片 hook 森林：工具一个注册表、提示词片段另一个注册表、
middleware 一套、生命周期回调又一套。每个可复用能力都要到处接线。

lovia 的 **plugin** 是一个对象，可以贡献任意组合：工具、system prompt 文本、
每轮 view injector、hooks 和护栏。runner 每次运行激活一次插件
（`await plugin.setup()`），运行结束后清理，并把贡献内容合并到上面固定循环的槽位。
插件不驱动控制流；中止、重试和 handoff 仍由循环掌控。

Skills、MCP、todo list 和长期记忆都是基于这一机制实现的插件，这也证明这条扩展轴
足够用了。见[插件](plugins.md)。

## 出错时会看到什么

所有框架异常都继承 `LoviaError`，所以 `except LoviaError` 会捕获 lovia 的错误，
不会误吞你自己的 bug。错误可以带一个可选的 `hint`，也就是追加在消息后的
“下一步该试什么”。

| 异常 | 何时抛出 |
| --- | --- |
| `UserError` | 框架配置错误（没有模型、选项不合法）——修调用点 |
| `ProviderError` | 模型 API 失败；包含 `vendor`、`status_code`、`retryable` |
| `ContextOverflowError` | prompt 超过上下文窗口，压缩也救不回来 |
| `ToolError` | 工具以适合结构化呈现的方式失败（通常由你抛出） |
| `InvalidToolArguments` | 工具参数没有通过 schema 校验（会反馈给模型修正） |
| `OutputValidationError` | 最终答案没有解析成 `output_type`（包括修复失败后） |
| `MaxTurnsExceeded` | 循环达到 `max_turns` 仍没有最终答案 |
| `BudgetExceeded` | `RunBudget` 的某项限制在运行中触发 |
| `RunCancelled` | `CancelToken` 被触发 |
| `GuardrailTripped` | 输入/输出护栏拒绝了某个值 |
| `MCPError` | MCP 服务器连接或调用失败 |

两个细节：工具抛出普通异常**不会**结束运行，错误会作为工具结果返回给模型，让它调整
策略（见[工具](tools.md)）；流式模式下，这些异常通过 `handle.result()` 暴露，
不会从迭代本身抛出。

## 可以依赖的设计约束

“简洁、轻量、可扩展、通用”的哲学，落到代码里就是这些不变量：

- **Agent 是配置。** `Agent` 上没有对话状态；可以共享，也可以轻松派生变体。
- **Transcript 永不重写。** 压缩只改变 view；session 和 checkpoint 只追加；
  完成后的运行不可变。
- **插件贡献能力，循环掌控流程。** 插件不能重试、中止或重新路由一次运行。
- **所有关联都靠 id，而不是位置。** 工具事件通过 `call.id` 配对；segment 和
  snapshot 通过 `run_id` 配对。并发不会打乱重要关系。
- **Provider 是 protocol。** 两个内置适配器通过 `httpx` 调 OpenAI 和 Anthropic；
  新接一个 provider 只需要实现 `Protocol`，不用继承一套基类。
- **核心保持很小。** `lovia` 导入时不带 web 栈，也不带工作区机制；这些层依赖核心，
  核心不反向依赖它们。

## 延伸阅读

- [快速上手](quickstart.md)：十分钟路径，也是这些概念的动机来源
- [运行 agent](running.md)：完整的 `Runner` 使用面
- [架构笔记](../architecture.md)：贡献者版本的本页，包含模块名和修改 lovia 本身时
  需要遵守的不变量
