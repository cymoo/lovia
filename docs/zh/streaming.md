# 流式输出

UI 不能等到 `RunResult` 出来才响应：文本生成时要立刻显示，工具开始调用时要更新状态，
审批请求一阻塞就要弹出来。`Runner.stream` 会为这些过程产出类型化事件；同一套事件
也驱动 [hooks](observability.md)，所以事件目录只需要学一次。

```python
from lovia import Runner, events

handle = Runner.stream(agent, "用一段话解释上下文窗口。")

async for ev in handle:
    match ev:
        case events.TextDelta(delta=d):
            print(d, end="", flush=True)
        case events.ToolCallStarted(call=c):
            print(f"\n[{c.name}...]", end="")
        case events.RunFailed(error=e):
            print(f"\n运行失败：{e}")

result = await handle.result()
```

事件是 `lovia.events` 里的普通 dataclass。可以用 `isinstance` 或 `match` 过滤。
如果你想订阅一整个类别，每个事件都派生自一组小的基类：
`RunEvent`、`TurnEvent`、`DeltaEvent`、`MessageEvent`、`ToolEvent`、
`TransitionEvent`、`ErrorEvent`、`ContextEvent`。

## 契约

三个保证决定了消费者应该怎么写：

1. **迭代不会因为运行失败而抛异常。** 每个流都会以且仅以一个终止事件结束：
   `RunCompleted` 或 `RunFailed`，然后停止。错误只会在 `await handle.result()`
   时变成异常。（任务取消和其他 `BaseException` 仍会传播。）
2. **Delta 在 `MessageCompleted` 之前都是临时的。** provider 中途失败后，runner
   可能丢弃已流出的部分输出并重启本轮；见下面的 `OutputDiscarded`。
3. **同一轮里的工具事件会交错。** 工具默认并发执行，所以请用 `ev.call.id` 关联事件，
   不要靠相邻位置判断。

## 事件目录

### 运行与 turn 生命周期

| 事件 | 字段 | 何时出现 |
| --- | --- | --- |
| `RunStarted` | `agent` | 第一个 turn 之前，出现一次 |
| `TurnStarted` | `agent`, `turn` | 每个 turn，在模型调用前 |
| `TurnEnded` | `agent`, `turn` | 每个 turn，在工具完成后 |
| `RunCompleted` | `result` | 终止事件：运行成功 |
| `RunFailed` | `error` | 终止事件：运行没有结果 |

### 模型输出

| 事件 | 字段 | 何时出现 |
| --- | --- | --- |
| `TextDelta` | `delta` | assistant 文本片段 |
| `ReasoningDelta` | `delta` | reasoning/思考片段，仅限会暴露它的 provider；适合折叠显示或弱化显示，不要依赖它做行为判断 |
| `OutputDiscarded` | — | 本 turn 已流出的 delta 作废；清掉你渲染的内容，之后会有一条新的流 |
| `MessageCompleted` | `entries` | 一个 assistant turn 已完整组装：包含它产生的新 `TranscriptEntry` |
| `UserMessageInjected` | `content`, `turn` | [mailbox](reliability.md#运行中追加指令) 消息被折入为用户 turn |

当 runner 通过重试或 fallback 从 provider 的中途流式错误中恢复时，会触发
`OutputDiscarded`
（见 [`RetryPolicy.restart_on_partial`](reliability.md#provider-重试)）。持久化
transcript 不受影响；它只由已完成 turn 组装出来。

### 工具与审批

| 事件 | 字段 | 何时出现 |
| --- | --- | --- |
| `ToolCallStarted` | `call` | 工具真正执行前 |
| `ToolCallCompleted` | `call`, `result`, `is_error`, `output` | 调用到达终态 |
| `ToolCallFailed` | `error`, `call` | 某个调用范围内的非终止错误（运行继续） |
| `ApprovalRequired` | `call`, `.approve()` / `.reject()` | 一个带门禁的工具正在等待决策 |

UI 最容易写错的细节：

- 调用可能在执行前被拒绝：未知工具、参数格式不对、审批被拒。这时会发出
  `ToolCallCompleted(is_error=True)`，但**不会**先发 `ToolCallStarted`。不要假设成对出现。
- `ToolCallCompleted.result` 是原始返回值，方便类型感知的消费者使用；`.output` 是模型
  实际收到的渲染字符串。
- 完成事件按**完成顺序**到达，不按请求顺序。
- `ToolCallFailed` 携带异常，供观测使用；模型看到的是配套的
  `ToolCallCompleted(is_error=True)` 字符串。终止运行的失败是 `RunFailed`，不是
  `ToolCallFailed`。
- 处理 `ApprovalRequired` 时，可以在循环继续交还控制权给 runner 前调用
  `ev.approve()` 或 `ev.reject()`；也可以稍后通过 `handle.approvals` 处理。
  当 turn 需要答案而请求仍未处理时，默认**拒绝**，所以健忘的 UI 不会挂住运行。
  同一 turn 里其他调用会在流停在审批事件上时继续执行。完整流程见[人工介入](human-in-the-loop.md)。

### 转移与上下文

| 事件 | 字段 | 何时出现 |
| --- | --- | --- |
| `HandoffOccurred` | `from_agent`, `to_agent` | 控制权[移交](multi-agent.md)到另一个 agent |
| `ContextCompacted` | `session_id`, `entries_before`, `entries_after`, `notice` | [上下文策略](context.md)为本 turn 生成了压缩后的 view |

`ContextCompacted.notice` 是 JSON-safe 的 `CompactionNotice`（原因、是否 reactive、
压缩前后 token、策略生成的 `detail` 行、可选 summary）。Web UI 重新加载已完成
session 时回放的也是同一个对象。

## 常见模式

**渐进文本 + 工具状态**：快速上手里的循环就是这个模式。并发工具 spinner 要用
`call.id` 维护 map。

**审批 UI**：遇到 `ApprovalRequired` 时暂停，展示 `ev.call.name` 和
`ev.call.arguments`，然后调用 `ev.approve()` / `ev.reject()`：

```python
async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ok = await confirm_dialog(ev.call.name, ev.call.arguments)
        ev.approve() if ok else ev.reject()
```

**服务端 fan-out**：把事件喂给自己的消息总线或 SSE 编码器。内置
[HTTP API](http-api.md) 正是这样做的；`lovia/web/sse.py` 是一个可用的翻译参考。

**没有 UI 的可观测性**：即使没人消费 stream，同一套事件也会到达
[hooks](observability.md)。做指标时更推荐 hooks，这样 instrumentation 不依赖谁在迭代。

## 容易踩的点

- **一个 handle 只能迭代一次。** 第二个 `async for` 会抛 `RuntimeError`；如果多个
  消费者都需要事件，请在下游自己 fan out。
- **放弃的 stream 不是已完成运行。** 中途 `break` 会停止驱动运行；之后
  `handle.result()` 会报告被放弃，而不是返回结果。需要结果时，请迭代到终止事件，
  或直接 `await handle`。
- **在 `MessageCompleted` 前把 delta 当临时内容渲染。** 一次 `OutputDiscarded`
  就能让天真的 UI 把同一段话显示两遍。

## 延伸阅读

- [运行 agent](running.md)：handle 和 result 表面
- [人工介入](human-in-the-loop.md)：处理审批的所有方式
- [可观测性](observability.md)：同一套事件，作为 hooks 使用
- 示例：[`03_streaming.py`](../../examples/03_streaming.py)
