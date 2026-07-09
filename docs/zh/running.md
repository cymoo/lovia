# 运行 agent

`Runner` 把一个 `Agent` 和一份输入变成一次运行。它本身无状态；所有单次运行状态
都存在它启动的循环里。它只暴露三个入口，区别只在于你如何消费这次运行。

```python
from lovia import Runner

result = await Runner.run(agent, "写一段发布说明。")      # 跑到完成
result = Runner.run_sync(agent, "总结这个文件。")         # 脚本 / REPL
handle = Runner.stream(agent, "解释上下文压缩。")         # 边运行边消费事件
```

`agent.run(...)` / `agent.run_sync(...)` / `agent.stream(...)` 是同一组调用的实例方法形式。

## 三个入口

**`Runner.run(agent, input, **options) -> RunResult`**：等待运行完成并返回最终结果。
失败会抛异常（`GuardrailTripped`、`BudgetExceeded`、`ProviderError` 等，见
[错误清单](concepts.md#出错时会看到什么)）。

**`Runner.run_sync(...)`**：做同样的事，但用 `asyncio.run()` 包起来，适合尚未使用
async 的代码。如果在已经运行的事件循环里调用，会抛 `UserError`，hint 里会告诉你
改用 `await Runner.run(...)`。

**`Runner.stream(...) -> RunHandle`**：启动运行，并返回一个 handle。它既是
**异步可迭代对象**（产生类型化事件），也是 **awaitable**（得到最终结果）：

```python
handle = Runner.stream(agent, "分析这些日志。")

async for ev in handle:          # 运行失败不会从这里抛出
    ...

result = await handle.result()   # 返回 RunResult，或抛出运行错误
```

迭代是一次性的；第二次 `async for` 会抛 `RuntimeError`。每个流都会以且仅以一个
终止事件结束：`RunCompleted` 或 `RunFailed`。`await handle` 是
`await handle.result()` 的简写；如果还没人迭代这个流，`result()` 会自己驱动它跑到
完成。`handle.cancel()` 可以在没有预先传 `CancelToken` 的情况下请求协作式取消；
`handle.approvals` 是[审批通道](human-in-the-loop.md#通过审批通道处理)。
事件本身见[流式输出](streaming.md)。

## 运行选项

三个入口都接受同一组关键字：

| 选项 | 默认值 | 作用 |
| --- | --- | --- |
| `context` | `None` | 你的依赖对象，会作为 `ctx.deps` 暴露（见 [Agent](agents.md#每次运行的依赖)） |
| `output_type` | `None` | 本次运行覆盖 agent 的[输出类型](structured-output.md) |
| `extra_instructions` | `None` | 本次运行追加到 system prompt 的内容，渲染在 agent 自身 instructions 后；handoff 到的每个 agent 都会重新应用 |
| `max_turns` | `50` | 模型轮次的硬上限；超过会抛 `MaxTurnsExceeded` |
| `budget` | `None` | 限制本次运行可消耗资源的 `RunBudget`（见[可靠性](reliability.md)） |
| `cancel_token` | `None` | 预先接入的协作式取消（见[可靠性](reliability.md#取消)） |
| `mailbox` | `None` | 运行中追加指令的通道（见[可靠性](reliability.md#运行中追加指令)） |
| `retry` | agent 的配置 | 本次调用覆盖 provider 重试策略 |
| `context_policy` | agent 的配置 | 本次调用覆盖[上下文策略](context.md) |
| `session` + `session_id` | `None` | 对话持久化（见 [Session 与 Checkpoint](sessions-and-checkpoints.md)） |
| `checkpoint` | `None` | 崩溃恢复和幂等运行（见 [Session 与 Checkpoint](sessions-and-checkpoints.md#checkpoint)） |
| `tracer` | `None` | 本次运行的 tracing（见[可观测性](observability.md#tracing)） |

`retry` 和 `context_policy` 是两个**应对策略**覆盖项。它们默认使用 agent 配置，而且
**初始** agent 的应对策略会贯穿整个运行，handoff 后也一样。其余选项是**限制和外部接入点**，
agent 侧没有对应项。

## 输入

`input` 可以是字符串（一条用户消息），也可以是 `Message` 列表，用来以多条消息开始：

```python
from lovia import Runner, system, user

result = await Runner.run(
    agent,
    [
        system("用海盗口吻回答。"),   # 额外 system 消息，会保存在 transcript 中
        user("我们驶向哪里？"),
    ],
)
```

### 图片和文件

消息内容除了字符串，也可以是类型化 part 列表：`TextPart`、`ImagePart`、`FilePart`。
provider 会把它们转换成自己的请求格式：

```python
from lovia import ImagePart, Runner, TextPart, user

result = await Runner.run(
    agent,
    [
        user(
            [
                TextPart("这张截图里有什么？"),
                ImagePart.from_path("shot.png"),
            ]
        )
    ],
)
```

（`user(...)` 也接受普通字符串或单个 part；但 part **列表**必须包含类型化 part，
列表里的普通 `str` 不会被自动转换。）

- `ImagePart(url=...)` 或 `ImagePart(data=..., mime_type=...)`：`url` / `data`
  必须且只能选一个；base64 `data` 需要 `mime_type`。`ImagePart.from_path()`
  会读取并编码本地文件，根据后缀推断 MIME 类型。可选 `detail="low"|"high"|"auto"`。
- `FilePart`：同样的形状，再加 `filename`；构造器有 `from_path`、`from_bytes`、
  `from_base64`、`from_url`。URL part 是 provider 原生引用，lovia 不会替你下载。

## 结果

| `RunResult` 字段 | 含义 |
| --- | --- |
| `output` | 最终答案：`str`，或本次运行 `output_type` 校验后的实例 |
| `entries` | **本次运行自己的** transcript：本次输入及其产生的所有内容，跨 handoff 也会包含 |
| `messages` | 从 `entries` 派生出的 chat 格式视图（有损） |
| `final_agent` | 产出最终答案的 agent；handoff 后可能和初始 agent 不同 |
| `usage` | 累计 token：`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens`、`total_tokens`；agent-as-tool 子运行也计入 |
| `turns` | 本次运行用了多少个模型轮次 |
| `finish_reason` | 最后一轮 provider 报告的结束原因；检查 `"stop"` 和 `"length"` 可发现被 `max_tokens` 截断的答案 |

`entries` 有意不包含 system prompt 和之前的 session 历史，因此无论运行是刚刚完成，
还是从 checkpoint 重建出来，它都一致。要看完整对话，可以在 hook 里读取
`ctx.entries`，或运行结束后调用 `session.load()`。

## 容易踩的点

- **`RunResult.entries` 不是完整 transcript**：它只是本次运行的增量。用它渲染
  “整段对话”的代码会不小心丢掉之前的历史；请用 session。
- **`finish_reason` 可能是 `None`**：provider 没报时如此；从已完成 checkpoint
  重放结果时也如此，因为 snapshot 不持久化它。
- **模型回复既没有内容也没有工具调用时，运行会完成**，输出为空字符串（会记录 warning 日志）。
  这几乎总是 provider 抖动或 `max_tokens` 截断；在相信空答案之前，先检查 `finish_reason`。
- **`run_sync` 拥有事件循环**：它拒绝在现有事件循环里运行。notebook 里如果已经有
  正在运行的事件循环，请用 `await Runner.run(...)`。

## 延伸阅读

- [流式输出](streaming.md)：`Runner.stream` 背后的事件清单
- [Session 与 Checkpoint](sessions-and-checkpoints.md)：持久化选项
- [可靠性](reliability.md)：预算、取消、运行中追加指令、重试
- 示例：[`01_hello.py`](../../examples/01_hello.py)，
  [`06_multimodal.py`](../../examples/06_multimodal.py)
