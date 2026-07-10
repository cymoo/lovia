# 可靠性

Agent 运行失败大致有两类原因：基础设施抖动（429、流断开）和行为失控（工具调用循环、预算爆炸）。
lovia 把对应的控制项分开，并遵循一条放置规则：

- **应对策略**：基础设施出问题时 agent 如何应对，放在 `Agent` 上，每次运行都会继承：
  `retry`、`default_tool_retries` / `default_tool_timeout`、`context_policy`。
- **限制**：一个请求最多能花多少，是 `Runner.run` 的参数，没有 agent 侧对应项：
  `max_turns`、`budget`、`cancel_token`。

```python
from lovia import Agent, RetryPolicy, RunBudget, Runner

agent = Agent(name="analyst", model="glm-5.2",
              retry=RetryPolicy(max_attempts=2))          # 应对策略

result = await Runner.run(
    agent,
    "分析这些日志。",
    budget=RunBudget(max_tool_calls=20, max_seconds=60),  # 限制
)
```

如果某个请求确实特殊，可以按调用覆盖应对策略（`Runner.run(..., retry=...,
context_policy=...)`）。**初始** agent 的应对策略贯穿整个运行，包括 handoff 之后。

## Provider 重试

重试**默认开启**：`Agent.retry` 默认是 `RetryPolicy()`，也就是总共 4 次尝试（3 次重试），带
jitter 的指数退避大约是 1s / 2s / 4s，每次等待上限 30s。`retry=None` 会完全关闭 provider
重试；`RetryPolicy(max_attempts=1)` 是每次运行层面的等价写法。

| `RetryPolicy` 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_attempts` | `4` | 每个 provider 的总调用次数（第一次算 1） |
| `restart_on_partial` | `True` | 从中途流式失败中恢复时，丢弃部分输出并重新执行本轮流式调用 |
| `backoff_base` / `backoff_max` | `1.0` / `30.0` | 指数退避，±50% jitter |
| `retry_on` | 可重试 `ProviderError` | 判定什么算临时错误的谓词 |

哪些错误算临时错误，由 [provider 适配器](providers.md#网络超时代理tls) 判定：HTTP 408/429/5xx、网络超时和
中途断连可重试；4xx 配置错误不重试；`ContextOverflowError` 永不重试，而是进入
[reactive compaction](context.md)，修正真正的问题。

**`restart_on_partial`** 是需要注意的开关：长运行里 provider 发出半段内容后中途断开并不少见。开启时
（默认），runner 会丢弃这个不完整轮次，并发出 [`OutputDiscarded`](streaming.md#模型输出)，让 UI
清掉已渲染内容，然后从头重新流式执行。transcript 只由已完成轮次组装，所以不会被污染。关闭时，
中途流式错误会立刻传播。

**供应商级故障转移**有意不做成 agent loop 的特性：把 `base_url` 指向路由网关
（LiteLLM、OpenRouter 等）由它在 server side 切换，或者换个模型对同一 session 重跑失败的请求。

工具级重试是另一套机制，默认关闭：用每工具 `@tool(retries=..., timeout=...)`，或 agent 级
`default_tool_retries` / `default_tool_timeout`（见[工具](tools.md#重试与超时)）。

## 预算

`RunBudget` 给一次运行设置硬上限。runner 会在轮次之间、每次模型回复后，以及每个工具调用的
preflight 时检查它：

| 字段 | 限制 |
| --- | --- |
| `max_input_tokens` / `max_output_tokens` / `max_total_tokens` | 累计 token |
| `max_tool_calls` | **请求的**工具调用数；被拒绝的也算，所以模型反复请求错误工具名也会撞上限 |
| `max_seconds` | 实际耗时，从第一次检查开始 |

语义是：触发预算后，会在下一个安全点抛 `BudgetExceeded`。已经在跑的工具调用可以**完成并持久化**
（触发预算会停止**分发**新工作，不会杀掉已经运行的工作）。一个预算实例带有单次运行状态
（时钟、调用计数），所以**每次运行都要创建新的**。在
[agent-as-tool](multi-agent.md#agent-as-tool) 子运行里，子运行自己的预算耗尽会变成工具错误结果，
让父 agent 处理，而不是结束父运行。

`max_turns`（默认 50）是最简单的限制：超过就抛 `MaxTurnsExceeded`。

## 取消

取消是协作式的，通过 token 表达。runner 在轮次之间、每次 preflight，以及每个工具结果完成后检查：

```python
from lovia import CancelToken, Runner

token = CancelToken()
handle = Runner.stream(agent, "长分析...", cancel_token=token)
# 任意地方：
token.cancel("用户点击停止")        # 或：handle.cancel("...")
```

运行会在下一个安全点以 `RunCancelled` 结束（stream 中表现为 `RunFailed`）；批量工具调用中途取消时，
仍在运行的同批调用也会被取消。token 在每次运行中始终存在，工具和 hooks 可以通过
`ctx.cancel_token` 拿到，因此运行也可以**取消自己**（比如 hook 发现危险模式，或工具检测到不可恢复状态）。
子运行继承父运行的 token：一次取消停止整棵树。

取消做不到两件事：中断**同步**工具的 worker thread（线程会跑完，副作用可能在运行结束后发生），以及撤回
已经发给 provider 的请求。

## 运行中追加指令

取消的另一面是追加指令：`Mailbox` 把消息送**进**正在运行的 agent。runner 会在每轮开始时取出其中的消息，
并把每条消息作为普通用户消息追加进去：

```python
from lovia import Mailbox, Runner

mailbox = Mailbox()
handle = Runner.stream(agent, "分析这些日志。", mailbox=mailbox)
mailbox.push("重点看 14:00 左右的 5xx 峰值。")   # 下一轮可见
```

工具和 hooks 会通过 `ctx.mailbox` 拿到同一个通道。如果你没有提供，runner 会为本次运行创建一个；
因此运行可以在没有外部协调的情况下给自己追加指令：

```python
from lovia import RunContext, events
from lovia.hooks import AgentHooks

hooks = AgentHooks()

@hooks.on(events.TurnStarted)
def deadline(ev, ctx: RunContext):
    if ev.turn == 9:
        ctx.mailbox.push("最后一轮：用已有信息回答。")
```

更精确地说：

- 取消息只发生在**每轮开始**，不会在中途发生。`TurnStarted` hook 会在本轮取消息前触发，
  所以从这个 hook push 的消息会落到当前轮；其他地方 push 的消息会落到下一轮。
- 每条被取出的消息都会发出 [`UserMessageInjected`](streaming.md#模型输出)，并立即持久化
  （崩溃不会丢掉已消费消息）。
- `push()` 返回 token；`remove(token)` 可以撤回尚未被取出的消息。
- 运行结束时还排着的消息会留在**调用方提供的** mailbox 中（可以交给下一次运行）；runner 创建的默认
  mailbox 在运行后无法访问。最后一轮中 push 的消息不会被看到。
- [Agent-as-tool](multi-agent.md#agent-as-tool) 子运行拥有自己的 mailbox，不复用父运行的 mailbox。

## 容易踩的点

- **重试会在错误暴露前放大延迟。** 4 次尝试加退避，可能让一轮模型调用在失败前等约 10s。交互式 UI
  通常会把应对策略设成 `max_attempts=2`，再让用户自己重试。
- **`max_seconds` 不是 deadline。** 它在下一次**检查**时触发；60s 预算遇到 5 分钟工具调用，会在
  大约 5 分钟后才结束。真正 deadline 请结合每工具 `timeout=` 和你自己计时器触发的 cancel token。
- **预算不会跨你的手动重试自动重置。** 用同一个 `RunBudget` 实例重跑失败请求，会带着已经花掉的
  时钟和计数。请新建预算（这也是 agent-as-tool 每次调用复制预算的原因）。
- **追加的指令是*用户*消息。** 模型会像看待普通用户消息一样看待它。它不会抢占已经请求的工具调用，
  也会像其他内容一样持久化进 session。

## 延伸阅读

- [Provider 与模型](providers.md)：什么可重试，多供应商故障转移的去处
- [Session 与 Checkpoint](sessions-and-checkpoints.md)：跨进程崩溃恢复（重试是单次运行内恢复）
- 示例：[`14_reliability.py`](../../examples/14_reliability.py)，
  [`16_steering.py`](../../examples/16_steering.py)
