# 可观测性

无法观察，就无从排查。Agent 的问题往往出现在运行途中：某个工具耗时 40 秒，某一轮消耗了
3 万个 token。lovia 提供三类由轻到重的观测手段：**钩子（hooks）**用于响应事件，
**追踪（tracing）**用于记录带时间信息的 span，**日志（logging）**用于记录内置流程。

## 事件钩子

`AgentHooks` 是事件订阅器：按事件类型注册处理函数后，Runner 会将每个事件分派给对应函数。
这些事件与[流式输出](streaming.md)产生的类型完全相同，因此即使没有消费者读取事件流，
观测逻辑仍会正常工作。

```python
from lovia import Agent, RunContext, events
from lovia.hooks import AgentHooks

hooks = AgentHooks()


@hooks.on(events.ToolCallStarted)
async def log_tool(ev: events.ToolCallStarted, ctx: RunContext):
    print("→", ev.call.name, "in session", ctx.session_id)


@hooks.on((events.RunCompleted, events.RunFailed))   # tuple 会同时注册两个类型
def at_end(ev, ctx):
    metrics.count("runs", tags={"ok": isinstance(ev, events.RunCompleted)})


@hooks.on_any
def firehose(ev, ctx):
    audit_log.write(type(ev).__name__)


agent = Agent(..., hooks=hooks)
```

契约：

- 每个处理函数都以 `handler(event, ctx)` 调用，也就是事件加本次运行的实时
  [`RunContext`](concepts.md#runcontext访问运行状态)（`session_id`、活跃 Agent、累计用量、
  transcript、cancel token、mailbox）。处理函数可以同步或异步。
- 按具体类型注册，但用 `isinstance` 匹配；订阅基类（如 `events.ToolEvent`）会捕获整个家族。
  同一类型可有多个 handler，按注册顺序执行；catch-all 最先执行。
- **失败时放行**：处理函数抛异常会被记录为 warning 日志（带 traceback）并跳过。坏掉的指标不应该中止被观察的运行。
- 顺序保证：事件在循环的单一派发点按发出顺序到达 hooks，和 stream 消费者看到的顺序一致。
- hooks 不只是观察者：`ctx` 是实时句柄，所以处理函数可以向 `ctx.mailbox` push（[运行中追加指令](reliability.md#运行中追加指令)），
  或触发 `ctx.cancel_token`。

[插件](plugins.md)也可以贡献自己的 `AgentHooks`，和 agent 自己的 hooks 一起派发。[Memory](memory.md)
就是这样在运行结束时触发整理。

## 链路追踪

hooks 告诉你**发生了什么**；span 告诉你**什么花了多久，处在哪个嵌套范围里**。`Tracer` protocol
只有一个方法：`span(name, **attributes)`，返回 context manager。向运行传入 tracer 时，runner
会发出四类 span：

| Span | 属性 |
| --- | --- |
| `run` | `agent`, `run_id`（结束时再加 `turns`, `total_tokens`, `resumed`） |
| `model_call` | `model`, `turn` |
| `tool_call` | `name`, `call_id` |
| `handoff` | `from_agent`, `to_agent` |

```python
from lovia import Runner
from lovia.tracing import ConsoleTracer

result = await Runner.run(agent, "...", tracer=ConsoleTracer(min_duration_ms=5))
```

内置三个实现：`NoopTracer`（默认，让观测几乎没有成本）、`ConsoleTracer`
（通过 `logging` 输出缩进树，适合本地调试）、`InMemoryTracer`（记录 `RecordedSpan`，供测试断言）。
生产中请接入自己的后端，如 OpenTelemetry、Logfire：实现这个小 protocol 即可
（`span()` 返回有 `set_attribute` / `record_exception` 的对象）。

tracer 是**运行级**开关，不是 agent 字段：它会跨 handoff 作用到当前活跃 agent；
[agent-as-tool](multi-agent.md#agent-as-tool) 子运行会继承它，所以子 span 会接入父 trace。

## 运行日志

lovia 在 `lovia` logger 下记录结构化过程日志：`run.start`、
`model.done: turn=2 tokens=1841(in=1520 out=321) …`、`tool.start`/`tool.error`、
`run.handoff`、`context.overflow`、`run.done`。默认挂 `NullHandler`，库在你要求前保持安静。
脚本和 notebook 可以这样开启：

```python
from lovia import enable_logging

enable_logging()                      # stderr 上 INFO；TTY 时带颜色
enable_logging("DEBUG", color=False)  # 更多细节，无 ANSI
```

`enable_logging` 是幂等的（再次调用会替换自己加的 handler），遵守 `NO_COLOR`，默认不向 root logger
传播（uvicorn 下不重复打印；`propagate=True` 可以重新开启）。生产应用应该自己配置 `logging`，
不必使用这个 helper。

## 用量统计

每次运行都会累计 `Usage`：结果在 `result.usage` 上，运行中可从 `ctx.usage` 看到，子运行会向上汇总：

| 字段 | 含义 |
| --- | --- |
| `input_tokens` | **完整** prompt 大小，包含已缓存 tokens |
| `output_tokens` | completion tokens |
| `cache_read_tokens` / `cache_write_tokens` | [prompt cache](providers.md#提示词缓存) 对 input 的拆分 |
| `total_tokens` | input + output |

cache 字段是对 `input_tokens` 的**细分**，不额外相加。成本公式应类似
`(input - cache_read) * rate_in + cache_read * rate_cached + …`。要看每轮增量，可以在
`RunCompleted`/`TurnEnded` hook 里做 diff，或从 `model.done` 日志行读取每轮模型调用自己的 usage。

## 注意事项

- **hooks 在运行循环里同步派发。** 慢处理函数会拖慢运行；事件之间会 await 派发。指标请异步发送
  （队列 + worker），不要每个事件都阻塞 HTTP 调用。
- **hook 里的修改会生效。** `ctx.entries` 是实时 transcript，请按只读处理。安全的可变入口是设计好的：
  mailbox 和 cancel。
- **`ConsoleTracer` 给人看，不给程序解析。** 格式不保证版本。需要结构化数据时，请实现自己的 `Tracer`。
- **重放很安静。** 已完成运行的 [checkpoint 重放](sessions-and-checkpoints.md)只会重新发终止事件；
  每轮的钩子和 span 不会再次触发；用量仍会计入调用方。

## 延伸阅读

- [流式输出](streaming.md)：hooks 接收的完整事件清单
- [可靠性](reliability.md)：`ctx` 的控制能力
- 示例：[`11_hooks.py`](../../examples/11_hooks.py)
