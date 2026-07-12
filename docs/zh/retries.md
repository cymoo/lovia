# Provider 重试

限流、超时和流式连接中断都属于常见的临时故障。lovia 会在每个模型 Turn 外应用重试策略，
应用代码不必重复实现重试循环。

```python
from lovia import Agent, RetryPolicy, Runner

agent = Agent(
    name="analyst",
    model="<model>",
    retry=RetryPolicy(max_attempts=2),
)

result = await Runner.run(agent, "分析这些日志。")
```

`Agent.retry` 属于 Agent 的运行姿态，每次运行都会继承。只有某一次请求需要不同策略时，
才在 Runner 入口传入 `retry=`。初始 Agent 的策略控制整次 Run，包括 Handoff 后的 Turn。

## 重试策略

Provider 重试默认开启。`Agent.retry` 默认为 `RetryPolicy()`：最多尝试五次，并使用带随机
抖动的指数退避。单次 Run 可用 `RetryPolicy(max_attempts=1)` 关闭；若要在 Agent 上默认
关闭，则设置 `retry=None`。

| `RetryPolicy` 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `max_attempts` | `5` | Provider 调用总次数，第一次也计数 |
| `restart_on_partial` | `True` | 丢弃未完成的流式文本并重新开始当前 Turn |
| `backoff_base` | `1.0` | 初始退避秒数 |
| `backoff_max` | `30.0` | 两次尝试之间的最大等待秒数 |
| `retry_on` | 可重试的 `ProviderError` | 判断错误是否为临时故障的谓词 |

Provider 适配器通常把 HTTP 408、429、5xx、网络超时和流中断标为可重试；配置错误通常
不可重试。`ContextOverflowError` 会进入[上下文压缩](context.md)，不走重试策略。

## 流式输出中断

启用 `restart_on_partial` 时，Runner 会发出
[`OutputDiscarded`](streaming.md#模型输出)，丢弃未完成的模型 Turn，再从头开始。权威
Transcript 只接收完整 Turn，因此半段文本不会变成历史。UI 收到该事件时应清除已经渲染的文本。

如果重复显示流式响应比直接失败更糟，可以把该选项设为 `False`。

## Tool 重试是另一套机制

Provider 重试会重复模型请求；Tool 重试只重复一次工具调用，并且默认关闭。可通过
`@tool(retries=..., timeout=...)` 为单个工具配置，或使用 Agent 的
`default_tool_retries` 和 `default_tool_timeout`。详见[工具](tools.md#重试与超时)。

!!! tip "交互场景减少重试"

    五次尝试可能带来明显等待。交互应用通常使用 `RetryPolicy(max_attempts=2)`，错误可见后
    再让用户决定是否重试。

## 延伸阅读

- [Provider 与模型](providers.md)：网络和 Provider 行为
- [预算与限制](budgets.md)：限制一次 Run 的成本
- [Session 与 Checkpoint](sessions-and-checkpoints.md)：跨进程故障恢复
- 示例：[`14_reliability.py`](../../examples/14_reliability.py)
