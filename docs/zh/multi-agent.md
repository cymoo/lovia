# 多 Agent

lovia 里的多 agent 组合刻意保持小而清楚：两个原语，底层都实现为普通工具，没有编排 DSL。
**Handoff** 会移交控制权：专家继续同一段对话。**Agent-as-tool** 是委派：子 agent 回答
一个有边界的问题，父 agent 接着往下走。更大的模式都用普通 Python 组合它们。

## Handoff

```python
from lovia import Agent, Runner

billing = Agent(name="billing", instructions="处理账单问题。", model="openai:gpt-5.5")
support = Agent(name="support", instructions="处理技术问题。", model="openai:gpt-5.5")

triage = Agent(
    name="triage",
    instructions="把用户转给合适的专家。",
    model="openai:gpt-5.5",
    handoffs=[billing, support],
)

result = await Runner.run(triage, "我被重复扣款了。")
print(result.final_agent.name)   # "billing"
```

`handoffs` 里的每个条目都会变成一个 `transfer_to_<name>` 工具（名称会 slugify 以符合
provider 语法：ASCII、64 字符，必要时加稳定 digest 后缀）。模型调用这个工具时，循环会
切换活跃 agent 并继续。

**目标 agent 会看到什么。** 对话会跟着 handoff 过去：新 agent 拿到完整的既有上下文，
包括历史、工具调用和结果。唯一变化是开头的 system prompt 会重新渲染成目标 agent 自己的
内容（instructions、workspace、plugins、结构化输出契约）。运行级
`extra_instructions` 会重新应用到 handoff 到达的每个 agent。

**还会改变什么。** 目标 agent 的 provider、工具、插件和工作区会重新解析（插件会运行
自己的 `setup()`）；会触发一个 `HandoffOccurred` 事件（派发给两个 agent 的 hooks）。
不变的是运行骨架：`max_turns`、预算、cancel token、mailbox、session、checkpoint，以及
**初始** agent 的 retry/context 姿态都会延续。

### 自定义 handoff

用 `Handoff` 包装目标 agent，可以控制工具：

```python
from lovia import Agent, Handoff

triage = Agent(
    name="triage",
    model="openai:gpt-5.5",
    handoffs=[
        Handoff(
            target=billing,
            description="账单：退款、重复扣款、发票、支付方式。",
            on_handoff=lambda args, ctx: audit_log(ctx.session_id, args.get("reason")),
        ),
        support,   # 普通 agent 也可以混用
    ],
)
```

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `target` | 必填 | 要转交给哪个 agent |
| `name` | `transfer_to_<slug>` | 覆盖工具名 |
| `description` | 通用转交文本 | **路由信号**。父 agent 需要在相似专家间选择时，请写清目标专长 |
| `on_handoff` | `None` | handoff 触发时调用的同步或异步回调 `(args, ctx)`；`args` 携带模型可选的 `reason` |

默认 description 故意很简略（只有 agent 名称），所以 `description` 是让路由可靠的关键设置。

### Handoff 语义

- **第一个 handoff 胜出。** Handoff 工具永远作为[屏障](tools.md#并发执行与屏障)执行，
  不会和其他调用并发；同一 turn 的第二个 handoff 会在它的 `on_handoff` 副作用发生前
  被拒绝。
- **切换发生在 turn 结束时。** 本 turn 剩余工具结果会先处理；下一次模型调用才是目标 agent。
- **恢复能穿过 handoff。** [Checkpoint](sessions-and-checkpoints.md) 会按名称记录
  **活跃** agent；恢复时从入口 agent 的 handoff 图中重新解析并继续。
- **Handoff 不会嵌套运行。** 无论转交链多深，它仍然是一次运行、一份 transcript、一个预算。

## Agent-as-tool

委派而不是转交：子 agent 在自己的循环里运行，只看到交给它的 prompt（绝不会看到父 agent
历史），最终输出作为工具结果返回：

```python
summarizer = Agent(
    name="summarizer",
    instructions="用五个要点总结文本。",
    model="openai:gpt-5.5",
)

manager = Agent(
    name="manager",
    instructions="需要总结时，把任务委派出去。",
    model="openai:gpt-5.5",
    tools=[summarizer.as_tool(description="总结一段文本。")],
)
```

`agent.as_tool(*, name=None, description=None, max_turns=50, budget=None,
retry=None, context_policy=None)`：

- 工具默认名为 `ask_<slug>`，只接受一个由模型控制的参数：`input`，即被委派的 prompt。
  执行策略关键字由**你**固定，不暴露给模型。尤其要给委派 agent 绑定 `max_turns`，
  因为它有自己的循环。
- `budget` 每次调用都会复制一份，所以限制作用于每个子运行，而不是跨调用累计。
- 子运行会**继承**父运行的 `context`（deps）、`cancel_token`（一次取消停止整棵树）和
  tracer（span 接到同一条 trace）；token 用量会折入父运行的 `usage`。
- 子运行拥有**自己的 mailbox**。这不是父运行的 mailbox，因为从 mailbox 取消息会消费它，
  注入消息也只应发给一段对话。
- 子运行耗尽自己的预算时，会作为工具错误结果反馈给父 agent，让父 agent 处理。这是可恢复
  的委派失败，不是结束父运行的失败（见[错误语义](tools.md#错误语义)）。

## 怎么选择

| 你想要 | 使用 |
| --- | --- |
| 用户接下来继续和专家对话 | handoff |
| 只要一个有边界子任务的答案，然后继续 | agent-as-tool |
| 专家看到完整对话 | handoff |
| 隔离：子 agent 不应看到父历史 | agent-as-tool |
| 最终答案归属专家（`result.final_agent`） | handoff |
| 多个委派，甚至并发委派 | agent-as-tool |

更大的模式，如链式调用、路由、并行化、orchestrator-worker、evaluator loop，都不需要框架
支持：用 `Runner.run` 外面的普通 Python 写即可。
[`examples/workflows/`](../../examples/workflows/) 目录用一页代码实现了 Anthropic
*Building effective agents* 里的每个模式。

## 容易踩的点

- **Handoff 目标需要可发现的 description。** 两个专家如果都用默认 description，
  路由视角看起来几乎一样；误路由首先是 prompt 问题，其次才是框架问题。
- **被覆盖的 `output_type` 跟着运行，而不是跟着 agent。**
  `Runner.run(..., output_type=...)` 覆盖会绑定 handoff 到达的每个 agent；没有覆盖时，
  各 agent 使用自己的 `output_type`。triage → specialist 链如果输出类型不同，契约会在运行中变化。
- **非 ASCII agent 名会生成 digest 工具名。** `transfer_to_agent_a1b2c3d4` 能正常路由，
  但日志不好读；需要可读名称时设置 `Handoff(name=...)` / `as_tool(name=...)`。
- **很深的 agent-as-tool 层级会悄悄放大成本。** 用量向上折叠，所以 `result.usage` 是**整个调用层级**
  的总量。根运行要按这个预算。

## 延伸阅读

- [工具](tools.md)：两个原语底层都是普通工具
- [Session 与 Checkpoint](sessions-and-checkpoints.md)：跨 handoff 恢复
- 示例：[`07_handoff.py`](../../examples/07_handoff.py)，
  [`08_agent_as_tool.py`](../../examples/08_agent_as_tool.py)，
  [`workflows/`](../../examples/workflows/)
