# 多 Agent

lovia 提供 Handoff、Agent-as-tool 和 Subagents 三种组合方式，不引入编排 DSL。

## 按任务选择方式

| 你的任务 | 使用 | 目标 Agent 看到什么 | 结果如何返回 |
| --- | --- | --- | --- |
| 让专家接手后续对话 | Handoff | 完整对话历史 | 专家在同一次 Run 中继续回答 |
| 主 Agent 需要先拿到子任务结果 | Agent-as-tool | 本次委派的提示词 | 作为 Tool 结果返回 |
| 独立任务可以在后台完成 | Subagents | 本次委派的提示词 | 稍后送回父对话，也可主动等待 |

## Handoff

```python
from lovia import Agent, Runner

billing = Agent(name="billing", instructions="处理账单问题。", model="<model>")
support = Agent(name="support", instructions="处理技术问题。", model="<model>")

triage = Agent(
    name="triage",
    instructions="把用户转给合适的专家。",
    model="<model>",
    handoffs=[billing, support],
)

result = await Runner.run(triage, "我被重复扣款了。")
print(result.final_agent.name)   # "billing"
```

`handoffs` 中的每个条目都会注册为 `transfer_to_<name>` 工具。工具名会被规范成
Provider 接受的格式：仅使用 ASCII，最长 64 个字符，必要时附加稳定的摘要后缀。
模型调用这个工具后，运行循环会切换到目标 Agent 并继续。

**目标 Agent 可以看到哪些内容。** Handoff 会把对话历史一并移交，包括此前的消息、
工具调用和工具结果。只有开头的系统提示会按目标 Agent 重新生成，其中包含它自己的
instructions、Workspace、插件和结构化输出约束。运行级 `extra_instructions` 也会
重新应用到 Handoff 到达的每个 Agent。

**还会改变什么。** 系统会重新解析目标 Agent 的 Provider、工具、插件和 Workspace，
并执行各插件的 `setup()`；同时触发 `HandoffOccurred` 事件，交给移交双方的 hooks。
运行本身则保持不变：`max_turns`、预算、取消令牌、Mailbox、Session、Checkpoint，
以及初始 Agent 配置的重试策略和上下文策略都会沿用。

### 自定义 Handoff

需要自定义移交工具时，可以用 `Handoff` 包装目标 Agent：

```python
from lovia import Agent, Handoff

triage = Agent(
    name="triage",
    model="<model>",
    handoffs=[
        Handoff(
            target=billing,
            description="账单：退款、重复扣款、发票、支付方式。",
            on_handoff=lambda args, ctx: audit_log(ctx.session_id, args.get("reason")),
        ),
        support,   # 也可以直接传入 Agent
    ],
)
```

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `target` | 必填 | 要转交给哪个 Agent |
| `name` | `transfer_to_<slug>` | 覆盖工具名 |
| `description` | 通用移交说明 | **路由依据**。父 Agent 需要在相似专家间选择时，应写清目标专长 |
| `on_handoff` | `None` | Handoff 发生时调用的同步或异步回调 `(args, ctx)`；`args` 包含模型可选填的 `reason` |

默认 `description` 只有 Agent 名称，信息很少。需要可靠路由时，应明确填写目标 Agent
擅长处理的问题。

### Handoff 语义

- **只执行第一个 Handoff。** Handoff 工具始终作为[屏障](tools.md#并发执行与屏障)执行，
  不会与其他工具调用并发；同一轮里的第二个 Handoff 会在触发 `on_handoff` 之前
  被拒绝。
- **切换发生在当前轮次结束时。** 系统会先处理本轮剩余的工具结果，下一次模型调用
  才交给目标 Agent。
- **Handoff 后仍可恢复。** [Checkpoint](sessions-and-checkpoints.md) 会按名称记录
  当前 Agent；恢复时会从入口 Agent 的 Handoff 关系中找到它并继续运行。
- **Handoff 不会创建嵌套运行。** 无论连续移交多少次，始终只有一个 Run、
  一份 Transcript 和一套预算。

## Agent-as-tool

这是委派，不是移交：子 Agent 在独立的运行循环中处理任务，只能看到交给它的提示词，
无法访问父 Agent 的对话历史。完成后，结果会作为工具结果返回。

```python
summarizer = Agent(
    name="summarizer",
    instructions="用五个要点总结文本。",
    model="<model>",
)

manager = Agent(
    name="manager",
    instructions="需要总结时，把任务委派出去。",
    model="<model>",
    tools=[summarizer.as_tool(description="总结一段文本。")],
)
```

`agent.as_tool(*, name=None, description=None, max_turns=50, budget=None,
retry=None, context_policy=None)`：

- 工具默认名为 `ask_<slug>`，只接受一个由模型填写的参数 `input`，也就是交给子 Agent
  的提示词。`max_turns` 等执行策略由应用代码固定，不会暴露给模型。子 Agent 有自己的
  运行循环，因此尤其建议明确限制 `max_turns`。
- `budget` 每次调用都会复制一份，所以限制作用于每个子运行，而不是跨调用累计。
- 子运行会**继承**父运行的 `context`（deps）、`cancel_token`（一次取消停止整棵树）和
  Tracer（子 Span 会加入同一条 Trace）；Token 用量会计入父运行的 `usage`。
- 子运行拥有**独立的 Mailbox**，不会与父运行共用。Mailbox 中的消息一经读取就会被消费，
  而且每条注入消息只应属于一段对话。
- 子运行耗尽预算时，父 Agent 会收到一个工具错误结果并决定如何处理。这属于可恢复的
  委派失败，不会直接结束父运行，详见[错误语义](tools.md#错误语义)。

## 后台子 Agent

Agent-as-tool 会让父 Agent 等待；`Subagents` 则把任务放到后台。
`spawn_subagent` 启动任务后立即返回任务 ID，子 Agent 在同一事件循环中并发执行，
父 Agent 可以继续工作。任务完成后，报告会在轮次边界作为用户侧消息进入对话，
不会变成属于较早一轮的工具结果。

```python
from lovia import Agent, Runner, Subagents

researcher = Agent(name="researcher", instructions="调研并输出报告。",
                   model="<model>", tools=[...])

agent = Agent(
    name="assistant",
    model="<model>",
    plugins=[Subagents([researcher])],   # 或用 Subagents() 克隆当前 Agent
                                         # （不包含 plugins 和 handoffs）
)
result = await Runner.run(agent, "调研 X 和 Y，然后对比总结。")
```

父 Agent 会获得四个工具；系统提示还会在每轮注明后台任务的当前状态：

- `spawn_subagent(prompt, agent=...)`：用一段无需依赖父对话的完整提示词启动子 Agent；
  子 Agent 看不到父对话。正在运行的任务达到 `max_concurrent` 后，新任务会被拒绝。
- `wait_subagents(ids=None, timeout_seconds=60)`：等待指定任务完成或超时，并收取
  已完成任务的报告。如果报告已经自动进入消息队列、但还没有被模型读取，它会从
  队列中撤回并直接返回，因此同一份报告不会出现两次。
- `send_to_subagent(id, message)`：给运行中的子 Agent 发消息——在其下一个回合开始时
  作为用户消息注入（补一条遗漏的约束、收窄范围），无需重启任务。每次 spawn 都有自己的
  [steering 邮箱](cancellation.md#在运行中追加指令)；`run_child` 覆盖实现会通过 `ChildSpec.mailbox` 拿到它，
  并必须把它接入实际运行子 Agent 的方式。
- `cancel_subagent(id)`：请求子 Agent 停止；被取消的任务不会产生报告。

`Subagents(agents=(), deliver=None, max_concurrent=4, max_turns=50,
budget=None, max_result_chars=16_000, instructions=None)`：

- 子运行继承父运行的 `context`（deps）和 Tracer，Token 用量也计入父运行的
  `usage`，这一点与 `as_tool` 相同。每个子 Agent 使用独立的取消令牌；
  `budget` 则在每次启动任务时复制一份。
- **`deliver` 决定任务是否能脱离当前 Run。** 默认不传 `deliver`：报告进入
  当前 Run 的消息队列；Run 结束时，尚未完成的任务会被取消。内置提示会要求模型
  在结束前等待这些任务，或明确取消它们。传入 `deliver` 回调后，报告全部交给
  回调处理，子 Agent 也可以在父 Run 结束后继续运行。这种方式主要供 Web 服务等
  托管场景使用，详见 [Web UI](web-ui.md#后台子-agent)。
- 默认模式下，子 Agent 无人值守运行。没有人处理的工具审批会被**自动拒绝**，
  因此应只给它配置无需人工审批的工具。在 Web UI 中，后台任务是可查看的独立会话，
  可以直接处理其中的审批。
- `Subagents()` 未指定 `agents` 时，会以当前 Agent 为模板，但移除 `plugins`
  和 `handoffs`。这样既不会递归派生，也不会共享插件状态；模型、指令、工具和
  Workspace 保持不变。
- `run_child` 是面向服务层的高级扩展点，用来接管子 Agent 的执行过程。它接收
  `ChildSpec` 并返回子运行的 `RunResult`；Web 层借此把任务交给自己的运行管理器。
  应将它与 `deliver` 配套设置。若只替换 `run_child`，当前 Run 结束时，
  `Subagents` 无法取消由外部管理的任务。

## 组合更复杂的流程

链式调用、路由、并行处理、编排者—执行者和评估循环等更复杂的流程，都不需要额外的
框架抽象：在 `Runner.run` 外层用普通 Python 组合即可。
[`examples/workflows/`](../../examples/workflows/) 目录用一页代码实现了 Anthropic
*Building effective agents* 里的每个模式。

## 使用建议

- **为 Handoff 目标写清 `description`。** 如果两个专家都使用默认说明，路由 Agent
  几乎无法区分它们。遇到错误路由时，应先检查提示词和说明，再排查框架行为。
- **运行级 `output_type` 覆盖跟随 Run，而不是 Agent。**
  `Runner.run(..., output_type=...)` 指定的输出类型会应用到 Handoff 到达的每个 Agent。
  未指定时，各 Agent 使用自己的 `output_type`；如果分流 Agent 和专家的类型不同，
  输出约束会在移交后发生变化。
- **非 ASCII 的 Agent 名称会生成带摘要的工具名。**
  `transfer_to_agent_a1b2c3d4` 可以正常路由，但日志不易阅读。需要可读名称时，
  请设置 `Handoff(name=...)` 或 `as_tool(name=...)`。
- **Agent-as-tool 嵌套越深，成本越高。** 用量会逐层向上汇总，因此
  `result.usage` 表示整棵调用树的总用量；根 Run 的预算应按这个总量设置。

## 延伸阅读

- [工具](tools.md)：三种机制如何通过普通工具工作
- [Session 与 Checkpoint](sessions-and-checkpoints.md)：Handoff 后恢复运行
- 示例：[`07_handoff.py`](../../examples/07_handoff.py)，
  [`08_agent_as_tool.py`](../../examples/08_agent_as_tool.py)，
  [`30_subagents.py`](../../examples/30_subagents.py)，
  [`workflows/`](../../examples/workflows/)
