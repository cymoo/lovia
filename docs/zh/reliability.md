# 可靠性

Agent 运行失败大致有两类原因：基础设施不稳定（如 429、流式连接中断），以及行为失控
（如工具调用陷入循环、预算超支）。lovia 将两类控制项分开配置，并遵循一条简单原则：

- **处理策略**：基础设施出现问题时 Agent 如何应对，配置在 `Agent` 上，每次运行都会继承：
  `retry`、`default_tool_retries` / `default_tool_timeout`、`context_policy`。
- **资源限制**：单个请求最多可以消耗多少资源，通过 `Runner.run` 的参数设置，Agent 侧没有对应字段：
  `max_turns`、`budget`、`cancel_token`。

```python
from lovia import Agent, RetryPolicy, RunBudget, Runner, model_from_env

agent = Agent(name="analyst", model=model_from_env(),
              retry=RetryPolicy(max_attempts=2))          # 应对策略

result = await Runner.run(
    agent,
    "分析这些日志。",
    budget=RunBudget(max_tool_calls=20, max_seconds=60),  # 限制
)
```

如果某个请求需要特殊处理，可以在调用时覆盖处理策略（`Runner.run(..., retry=...,
context_policy=...)`）。**初始** agent 的应对策略贯穿整个运行，包括 handoff 之后。

## Provider 调用重试

重试**默认开启**：`Agent.retry` 默认是 `RetryPolicy()`，也就是总共 5 次尝试（4 次重试），带
jitter 的指数退避大约是 1s / 2s / 4s / 8s，每次等待上限 30s。`retry=None` 会完全关闭 provider
重试；`RetryPolicy(max_attempts=1)` 是每次运行层面的等价写法。

| `RetryPolicy` 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_attempts` | `5` | 每个 provider 的总调用次数（第一次算 1） |
| `restart_on_partial` | `True` | 从中途流式失败中恢复时，丢弃部分输出并重新执行本轮流式调用 |
| `backoff_base` / `backoff_max` | `1.0` / `30.0` | 指数退避，±50% jitter |
| `retry_on` | 可重试 `ProviderError` | 判定什么算临时错误的谓词 |

哪些错误算临时错误，由 [provider 适配器](providers.md#网络超时代理tls) 判定：HTTP 408/429/5xx、网络超时和
中途断连可重试；4xx 配置错误不重试；`ContextOverflowError` 永不重试，而是进入
[响应式压缩](context.md)，从根本上解决问题。

**`restart_on_partial`** 是需要注意的开关：长运行里 provider 发出半段内容后中途断开并不少见。开启时
（默认），runner 会丢弃这个不完整轮次，并发出 [`OutputDiscarded`](streaming.md#模型输出)，让 UI
撤销已经渲染的内容，然后重新开始流式输出。运行记录只包含已经完成的轮次，因此不会混入残缺内容。关闭时，
中途流式错误会立刻传播。

**供应商级故障转移**不属于 Agent 循环的职责：应将 `base_url` 指向路由网关
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

取消操作有两项限制：它无法中断**同步**工具的工作线程（线程仍会执行完毕，副作用可能在运行结束后发生），也无法撤回
已经发给 provider 的请求。

## 运行中追加指令

除了取消运行，还可以在运行过程中追加指令。`Mailbox` 会将消息发送给**正在运行的** Agent，Runner 在每轮开始时取出这些消息，
并把每条消息作为普通用户消息追加进去：

```python
from lovia import Mailbox, Runner

mailbox = Mailbox()
handle = Runner.stream(agent, "分析这些日志。", mailbox=mailbox)
mailbox.push("重点看 14:00 左右的 5xx 峰值。")   # 下一轮可见
```

工具和钩子可以通过 `ctx.mailbox` 访问同一个通道。如果调用方没有提供，Runner 会为本次运行创建一个；
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
  因此，在这个钩子中推送的消息会加入当前轮；从其他位置推送的消息则会在下一轮生效。
- 每条被取出的消息都会发出 [`UserMessageInjected`](streaming.md#模型输出)，并立即持久化
  （崩溃不会丢掉已消费消息）。
- `push()` 返回 token；`remove(token)` 可以撤回尚未被取出的消息。
- 运行结束时还排着的消息会留在**调用方提供的** mailbox 中（可以交给下一次运行）；runner 创建的默认
  mailbox 在运行后无法访问。最后一轮中 push 的消息不会被看到。
- [Agent-as-tool](multi-agent.md#agent-as-tool) 子运行拥有自己的 mailbox，不复用父运行的 mailbox。

## 注意事项

- **重试会在错误暴露前放大延迟。** 5 次尝试加退避，可能让一轮模型调用在失败前等约 15s。交互式 UI
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
