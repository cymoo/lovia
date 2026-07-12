# Agent

`Agent` 定义的是**运行内容**：名称、指令、模型、工具和策略。它本身不保存
对话状态，因此同一个实例可以同时处理任意数量的请求。需要为某次请求调整配置时，
用 `clone()` 派生一个变体即可，无须复制带有运行状态的对象。

```python
from lovia import Agent

agent = Agent(
    name="science-writer",
    instructions="你是一位科普作者，善于用生动的日常比喻讲清复杂的科学概念。",
    model="<model>",
)
```

## 字段

每个字段都有明确的默认值。`None` 不代表某个隐藏常量，只表示关闭、继承或自动创建。

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `name` | 必填 | 可读名称；也用于生成 handoff 工具名（`transfer_to_<name>`） |
| `instructions` | `""` | 基础 system prompt；可以是字符串，也可以是接收本次运行 `RunContext` 的 callable |
| `model` | `None` | `"vendor:model"` 字符串或 `Provider` 实例；没有模型时，运行会抛 `UserError` |
| `tools` | `[]` | 模型可以调用的[工具](tools.md) |
| `output_type` | `str` | 类型化最终输出；见[结构化输出](structured-output.md) |
| `output_repair` | `True` | 输出解析失败时修复一次；`False` 表示快速失败；也可以用 `OutputRepairStrategy` 自定义 |
| `handoffs` | `[]` | 模型可以[移交控制权](multi-agent.md)的 agent（或 `Handoff` 包装器） |
| `settings` | `ModelSettings()` | 传给 provider 的采样参数 |
| `retry` | `RetryPolicy()` | provider 重试策略（4 次重试、带 jitter 的退避）；`None` 表示关闭 |
| `context_policy` | `Compaction()` | 每次调用的 view 如何生成；见[上下文管理](context.md) |
| `workspace` | `None` | 受策略限制的文件/shell 工具；见[工作区](workspace.md) |
| `plugins` | `[]` | 能力包；见[插件](plugins.md) |
| `hooks` | `None` | 观察每个运行事件的 `AgentHooks`；见[可观测性](observability.md) |
| `approval_handler` | `None` | 程序化审批策略；见[工具审批](tools.md#工具审批) |
| `input_guardrails` / `output_guardrails` | `[]` | 有权停止运行的检查；见[护栏](guardrails.md) |
| `default_tool_retries` | `0` | 没有自行设置重试的工具使用这个值 |
| `default_tool_timeout` | `None` | 没有自行设置超时的工具每次尝试使用这个值 |
| `max_tool_output_chars` | `200_000` | 防止工具输出过大、导致运行记录无限膨胀的安全上限（见[工具](tools.md#输出截断)） |
| `tool_result_renderer` | `None` | agent 级工具结果渲染器；工具自身没有渲染器时使用 |

可靠性相关字段遵循一条明确的配置原则：**故障处理策略属于 Agent，资源限制属于单次运行**。
见 [Provider 重试](retries.md)和[预算与限制](budgets.md)。

## 指令

四种形式，从静态到完全动态：

**字符串**：最常见。

**callable**：把整个基础 prompt 变成动态内容。它接收本次运行的 `RunContext`
（也就是工具拿到的同一个句柄），可以是同步或异步函数：

```python
async def instructions(ctx) -> str:
    return f"你正在支持 {ctx.deps.plan} 套餐用户。回复要简短。"

agent = Agent(name="support", instructions=instructions, model="<model>")
```

**注册片段**：基础 prompt 保持静态，通过 `@agent.instruction` 装饰器追加动态片段。
片段会按注册顺序渲染在 `instructions` 之后，以空行分隔；返回 `""` 可以按条件跳过：

```python
agent = Agent(name="support", instructions="你是一名客服 agent。", model="<model>")

@agent.instruction
async def user_tier(ctx) -> str:
    return f"用户等级：{ctx.deps['tier']}" if ctx.deps else ""
```

**`with_instructions`**：纯函数式变体。它返回一个多了一个片段的 clone，不改变原对象。

最终渲染出的内容，也就是基础 prompt + 片段 + 每次运行追加的 `extra_instructions`，就是
模型看到的 system prompt，之后可以通过 `ctx.system_prompt` 观察。工作区 instructions、
插件 instructions，以及 provider 没有原生 JSON schema 支持时的结构化输出契约，会由
runner 追加在它后面。

> **动态 prompt 与 provider 缓存。** provider 会缓存 prompt 前缀；每次调用都变化的
> 片段（时间戳、请求 id）会让缓存每轮失效。尽量渲染稳定文本：写日期，不写精确时间
> （见[内置工具](built-in-tools.md#时间)里的 `current_date`）、用户等级而不是
> session id。易变细节更适合放在工具结果里。

## 派生不同配置

`clone()` 会返回一个只替换部分字段的副本，是派生单次请求配置或实验变体的推荐方式：

```python
strict = agent.clone(instructions="只回答带引用的内容。")
variant = agent.clone(model="<other-model>")
```

`@agent.instruction` 和 `clone()` 采用“注册时复制”的规则：派生前已注册的片段会复制到新实例中
（以不可变元组保存，不会共享可变状态）；派生后注册的片段只影响当前 Agent。建议在创建 Agent 后
立即注册所需片段；如果不希望修改原对象，可以使用
`with_instructions`。

## 每次运行的依赖

instructions、工具、hooks 或护栏在运行时需要的依赖，比如数据库连接池、当前用户，
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


agent: Agent[Deps] = Agent(name="support", model="<model>", tools=[open_tickets])

result = await Runner.run(agent, "我还有未处理工单吗？", context=Deps("u1", db))
```

泛型参数（`Agent[Deps]`、`RunContext[Deps]`）是给类型检查器看的；运行时
`ctx.deps` 就是你传入的对象（或 `None`）。工具通过把某个参数标注为 `RunContext`
来选择接收上下文；参数名无所谓，但最多只能有一个这样的参数。context 句柄上的其余
内容，包括 transcript、usage、mailbox、cancel token，见
[核心概念](concepts.md#runcontext访问运行状态)。

## 运行 Agent

`agent.run(...)`、`agent.run_sync(...)` 和 `agent.stream(...)` 只是对应
`Runner` 方法的便捷写法。Session、预算、Checkpoint 和运行中追加指令等完整参数，
见[运行 Agent](running.md)。

## 注意事项

- **`@agent.instruction` 会修改 agent**：这是为了装饰器易用性而保留的唯一例外。
  如果涉及 clone，片段注册发生在 clone 前还是后，决定谁会得到这个片段。
- **callable instructions 会在每一轮渲染 prompt 前执行，而不是只执行一次。**
  它们应该足够快，并且尽量确定；在 instructions callable 里做慢 I/O 会拖慢每次模型调用。
- **`Agent` 是普通 dataclass**：框架不会阻止你直接给字段赋值，但它假设 agent
  不会在运行中变化。把实例当成 frozen，用 `clone()` 改配置。

## 延伸阅读

- [运行 agent](running.md)：完整 run/stream 参数
- [Provider 与模型](providers.md)：`model=` 接受哪些形式
- 示例：[`01_hello.py`](../../examples/01_hello.py)，
  [`18_dependencies.py`](../../examples/18_dependencies.py)
