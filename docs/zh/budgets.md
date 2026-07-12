# 预算与限制

重试决定 Run 如何应对基础设施故障；限制决定单个请求最多可以消耗多少资源。因此它们应作为
Runner 参数传入，而不是放在可复用的 Agent 配置上。

```python
from lovia import Agent, RunBudget, Runner

agent = Agent(name="analyst", model="<model>")

result = await Runner.run(
    agent,
    "分析这些日志。",
    max_turns=12,
    budget=RunBudget(max_tool_calls=20, max_seconds=60),
)
```

## RunBudget

Runner 会在 Turn 之间、每次模型回复后，以及每个 Tool 调用的预检阶段检查预算。

| 字段 | 限制对象 |
| --- | --- |
| `max_input_tokens` | 累计输入 Token |
| `max_output_tokens` | 累计输出 Token |
| `max_total_tokens` | 累计输入与输出 Token |
| `max_tool_calls` | Tool 请求次数，包括被拒绝的调用 |
| `max_seconds` | 从第一次预算检查开始计算的墙钟时间 |

超限后会在下一个安全点抛出 `BudgetExceeded`。预算会停止分派新的 Tool 调用，但已经运行的
调用会完成并持久化结果。

!!! warning "每次 Run 创建新的预算"

    `RunBudget` 会保存开始时间和 Tool 调用计数。复用同一个实例，会把已经消耗的时间和次数
    带入下一次 Run。

[Agent-as-tool](multi-agent.md#agent-as-tool) 子 Run 使用独立复制的预算。子 Run 超限时，父 Run
会收到 Tool 错误并决定如何处理，而不是自动失败。

## Turn 限制

`max_turns` 默认为 `50`，耗尽后抛出 `MaxTurnsExceeded`。它是防止 Agent 反复调用工具、
始终无法产生最终答案的最直接保护。

## 时间限制是协作式的

`max_seconds` 只在安全点检查，并非硬截止时间。一个运行五分钟的 Tool 可能让 60 秒预算延迟
结束。需要真实截止时间时，应同时配置 Tool 的 `timeout=` 和[取消](cancellation.md)。

## 延伸阅读

- [Provider 重试](retries.md)：从临时模型故障中恢复
- [工具](tools.md#重试与超时)：单次 Tool 调用超时
- [取消与运行中引导](cancellation.md)：停止正在运行的 Run
