# 人工介入

人工参与有两个方向，对应两个机制。**Approval**：*runner* 在带门禁工具调用前暂停，等某个人
或策略做决定。**`ask_human`**：*模型* 向操作员提问，并等待答案。两者都按安全优先处理：
无人回答的审批默认拒绝；关闭的 channel 会让工具调用出错。

## 工具审批

用 `needs_approval` 给工具加门禁。它可以是 bool，也可以是基于已解析参数的谓词：

```python
from lovia import tool


@tool(needs_approval=True)
async def refund(order_id: str, amount_cents: int) -> str:
    """发起退款。"""
    return "refunded"


@tool(needs_approval=lambda args, ctx: args["amount_cents"] > 5_000)
async def discount(order_id: str, amount_cents: int) -> str:
    """应用折扣；小额折扣自动通过。"""
    return "applied"
```

当带门禁调用出现时，runner 会发出
[`ApprovalRequired`](streaming.md#工具与审批) 并等待。三条决策路径按顺序咨询：

**1. 流式消费者**：在事件循环中直接处理：

```python
from lovia import Runner, events

handle = Runner.stream(agent, "给订单 A123 退款。")

async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()          # 或 ev.reject()
```

**2. agent 的 `approval_handler`**：服务端策略，在消费者没做决定时咨询：

```python
agent = Agent(
    ...,
    approval_handler=lambda call, ctx: "ask" if call.name == "refund" else "allow",
)
```

它可以返回 `True`/`"allow"`、`False`/`"deny"`，或 `"ask"`（交回给消费者/channel）。同步或异步都可以。
handler 抛异常按拒绝处理。

**3. 默认：拒绝。** 如果 turn 需要答案时还没人决定，调用会被拒绝。运行永远不会因为忘了点对话框而挂住。
模型会看到 `"Tool {name} was not approved."`，然后自行调整。

### 通过审批通道处理

如果决策者不是 stream 消费者，比如一个 web endpoint、Slack bot 或另一个 task，可以通过 handle
的 channel 按 call id 处理：

```python
handle = Runner.stream(agent, "做维护操作。")
# 另一处，根据 ApprovalRequired 事件拿到 call id 后：
handle.approvals.approve(call_id)
handle.approvals.reject(call_id)
handle.approvals.release(decision=False)   # 收尾：处理所有未决请求
```

内置 [web server](http-api.md) 用的正是这个模式：`ApprovalRequired` 通过 SSE 发出去，
`POST /api/chat/approve` 调用 channel。

### 需要知道的语义

- **审批属于 preflight，而 preflight 按请求顺序串行执行。** 一个调用等待审批时，本 turn 中已经通过
  审批的并行调用会继续执行；排在它后面的调用会等到轮到自己。因此审批提示会按顺序一个个到达。
- **`needs_approval` 谓词抛异常时默认拒绝**：调用会被拒绝（同时给观察者发出带异常的
  `ToolCallFailed`），绝不会未经检查就执行。
- **非流式调用者**（`Runner.run`）看不到事件。请给 agent 提供 `approval_handler`，否则带门禁工具会被默认拒绝。
- **[工作区](workspace.md) 的 `ask` 决策走同一条通道**：一个审批 UI 就能覆盖你的工具、MCP 服务器、文件写入和 shell 命令。

## 询问人工

反方向：模型需要只有人知道的信息。

```python
from lovia import Agent, Runner
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()

agent = Agent(
    name="assistant",
    model="openai:gpt-5.5",
    tools=[ask_human(channel)],
)
```

模型调用 `ask_human(question)`；这个调用会阻塞，直到操作员侧回答。惯用消费者是一个循环：

```python
async for q in channel.questions():        # channel.close() 后结束
    channel.answer(q.id, await get_reply_somehow(q.question))
```

channel API：

| 方法 | 作用 |
| --- | --- |
| `questions()` | 异步迭代模型提出的问题（单消费者；迭代前的问题会排队） |
| `pending` | 轮询式快照：尚未回答的问题 |
| `answer(id, text)` | 解决问题，工具调用返回 `text` |
| `cancel(id, reason=...)` | 让某个调用以模型可见的 `ToolError` 失败 |
| `close(reason=...)` | 取消所有未决问题，结束 `questions()`，让未来提问失败；幂等 |

取消和关闭都会以工具错误结果暴露给模型，所以它能在没有答案的情况下继续，而不是让运行崩掉。
如果操作员可能离开，请配合每工具 timeout（`ask_human` 由工厂生成，可以用
`dataclasses.replace` 按 `@tool(timeout=...)` 语义包装或重建）。

Approval 问的是“我能做这件事吗？”答案是 yes/no。`ask_human` 问的是“我需要知道什么？”
答案是自由文本。如果你发现自己在 approval 里编码数据，其实需要 `ask_human`；如果
`ask_human` 回复永远是 yes/no，其实需要审批门禁。

## 容易踩的点

- **决策必须来自事件循环线程。** 两种 channel 都在 resolve `asyncio` future；从其他线程调用时，
  先跳回事件循环：`loop.call_soon_threadsafe(channel.answer, qid, text)`。
- **turn 已经继续后再 `ev.approve()` 不会生效**：该调用已经被默认拒绝逻辑拒绝。
  请在迭代交回控制权前决定，或通过审批 channel 处理。
- **审批决策不会持久化。** [恢复](sessions-and-checkpoints.md)时，未完成的带门禁调用会重新 preflight，
  并再次请求审批。

## 延伸阅读

- [流式输出](streaming.md)：`ApprovalRequired` 事件契约
- [工作区](workspace.md)：文件/shell ACL 的 `ask` 层
- [HTTP API](http-api.md)：SSE + POST 形式的审批
- 示例：[`12_approval.py`](../../examples/12_approval.py)，
  [`tools/04_human.py`](../../examples/tools/04_human.py)
