# Agent

`Agent` 描述的是**要运行什么**：名称、instructions、模型、工具、策略。它不保存
对话状态，所以一个实例可以服务任意数量的并发请求；每个请求需要的变体可以用
`clone()` 派生，而不是复制一份带运行状态的对象。

```python
from lovia import Agent

agent = Agent(
    name="writer",
    instructions="回答要具体、简洁。",
    model="glm-5.2",
)
```

## 字段

每个字段都有显式默认值。`None` 不会暗藏某个常量；它表示关闭、继承或自动创建。

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `name` | 必填 | 面向人的名称；也用于生成 handoff 工具名（`transfer_to_<name>`） |
| `instructions` | `""` | 基础 system prompt；可以是字符串，也可以是接收本次运行 `RunContext` 的 callable |
| `model` | `None` | `"vendor:model"` 字符串、`Provider` 实例，或用于 [fallback 链](providers.md#fallback-链)的列表；没有模型时运行会抛 `UserError` |
| `tools` | `[]` | 模型可以调用的[工具](tools.md) |
| `output_type` | `str` | 类型化最终输出；见[结构化输出](structured-output.md) |
| `output_repair` | `True` | 输出解析失败时修复一次；`False` 表示快速失败；也可以用 `OutputRepairStrategy` 自定义 |
| `handoffs` | `[]` | 模型可以[移交控制权](multi-agent.md)的 agent（或 `Handoff` 包装器） |
| `settings` | `ModelSettings()` | 传给 provider 的采样参数 |
| `retry` | `RetryPolicy()` | provider 重试策略（3 次重试、带 jitter 的退避）；`None` 表示关闭 |
| `context_policy` | `Compaction()` | 每次调用的 view 如何生成；见[上下文管理](context.md) |
| `workspace` | `None` | 受策略限制的文件/shell 工具；见[工作区](workspace.md) |
| `plugins` | `[]` | 能力包；见[插件](plugins.md) |
| `hooks` | `None` | 观察每个运行事件的 `AgentHooks`；见[可观测性](observability.md) |
| `approval_handler` | `None` | 程序化审批策略；见[人工介入](human-in-the-loop.md) |
| `input_guardrails` / `output_guardrails` | `[]` | 可以停止运行的检查；见[护栏](guardrails.md) |
| `default_tool_retries` | `0` | 没有自行设置重试的工具使用这个值 |
| `default_tool_timeout` | `None` | 没有自行设置超时的工具每次尝试使用这个值 |
| `max_tool_output_chars` | `200_000` | 防止工具输出失控撑大 transcript 的保险线（见[工具](tools.md#输出截断)） |
| `tool_result_renderer` | `None` | agent 级工具结果渲染器；工具自身没有渲染器时使用 |

和可靠性相关的字段遵循一个值得记住的规则：**应对策略放在 agent 上，限制放在运行上**。
见[可靠性](reliability.md)。

## Instructions

四种形式，从静态到完全动态：

**字符串**：最常见。

**callable**：整个基础 prompt 变成动态内容。它接收本次运行的 `RunContext`
（也就是工具拿到的同一个句柄），可以是同步或异步函数：

```python
async def instructions(ctx) -> str:
    return f"你正在支持 {ctx.deps.plan} 套餐用户。回复要简短。"

agent = Agent(name="support", instructions=instructions, model="glm-5.2")
```

**注册片段**：基础 prompt 保持静态，通过 `@agent.instruction` 装饰器追加动态片段。
片段会按注册顺序渲染在 `instructions` 之后，以空行分隔；返回 `""` 可以按条件跳过：

```python
agent = Agent(name="support", instructions="你是一名客服 agent。", model="glm-5.2")

@agent.instruction
async def user_tier(ctx) -> str:
    return f"用户等级：{ctx.deps['tier']}" if ctx.deps else ""
```

**`with_instructions`**：纯函数式变体。它返回一个多了一个片段的 clone，不改变原对象。

最终渲染结果，即基础 prompt + 片段 + 每次运行的 `extra_instructions` 追加内容，就是
模型看到的 system prompt，之后可以通过 `ctx.system_prompt` 观察。工作区 instructions、
插件 instructions，以及 provider 没有原生 JSON schema 支持时的结构化输出契约，会由
runner 追加在它后面。

> **动态 prompt 与 provider 缓存。** provider 会缓存 prompt 前缀；每次调用都会变化的
> 片段（时间戳、请求 id）会让缓存每轮失效。尽量渲染稳定文本：日期而不是精确时间
> （见[内置工具](built-in-tools.md#时间)里的 `current_date`）、用户等级而不是
> session id。易变细节更适合放在工具结果里。

## Clone 与变体

`clone()` 返回一个替换了部分字段的副本，是派生每次请求或每个实验变体的推荐方式：

```python
strict = agent.clone(instructions="只回答带引用的内容。")
variant = agent.clone(model="glm-5.2")
```

`@agent.instruction` 和 `clone()` 的边界是 **copy-on-register**：clone 之前注册的
片段会被带过去（作为不可变 tuple，没有共享可变状态）；clone 之后注册的片段只影响
注册所在的 agent。建议构造 agent 后马上注册片段；如果你完全不想变更原对象，就用
`with_instructions`。

## 每次运行的依赖

instructions、工具、hooks 或护栏在运行时需要的东西，比如数据库连接池、当前用户，
应该作为本次运行的 `context` 对象传入，而不是放在 agent 状态上：

```python
from dataclasses import dataclass

from lovia import Agent, RunContext, Runner, tool


@dataclass
class Deps:
    user_id: str
    db: "Database"


@tool
async def open_tickets(ctx: RunContext[Deps]) -> str:
    """列出当前用户的未关闭工单。"""
    rows = await ctx.deps.db.tickets(ctx.deps.user_id)
    return "\n".join(rows) or "没有未关闭工单。"


agent: Agent[Deps] = Agent(name="support", model="glm-5.2", tools=[open_tickets])

result = await Runner.run(agent, "我还有未处理工单吗？", context=Deps("u1", db))
```

泛型参数（`Agent[Deps]`、`RunContext[Deps]`）是给类型检查器看的；运行时
`ctx.deps` 就是你传入的对象（或 `None`）。工具通过把某个参数标注为 `RunContext`
来选择接收上下文；参数名无所谓，但最多只能有一个这样的参数。context 句柄上的其余
内容，包括 transcript、usage、mailbox、cancel token，见
[核心概念](concepts.md#runcontext唯一的运行句柄)。

## 运行 agent

`agent.run(...)`、`agent.run_sync(...)`、`agent.stream(...)` 都只是对应
`Runner` 方法的薄封装。完整参数，包括 session、预算、checkpoint、运行中追加指令等，
见[运行 agent](running.md)。

## 容易踩的点

- **`@agent.instruction` 会修改 agent**：这是为了装饰器易用性而保留的唯一例外。
  如果涉及 clone，片段注册发生在 clone 前还是后，决定谁会得到这个片段。
- **callable instructions 会在每一轮渲染 prompt 前执行，而不是只执行一次。**
  它们应该快，并且尽量确定；在 instructions callable 里做慢 I/O 会拖慢每次模型调用。
- **`Agent` 是普通 dataclass**：框架不会阻止你直接给字段赋值，但它假设 agent
  不会在运行中变化。把实例当成 frozen，用 `clone()` 改配置。

## 延伸阅读

- [运行 agent](running.md)：完整 run/stream 参数
- [Provider 与模型](providers.md)：`model=` 接受哪些形式
- 示例：[`01_hello.py`](../../examples/01_hello.py)，
  [`18_dependencies.py`](../../examples/18_dependencies.py)
