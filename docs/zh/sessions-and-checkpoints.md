# Session 与 Checkpoint

两个不同的持久化问题，对应两个存储。**session** 回答“这段对话到目前为止说过什么？”；
它是跨运行的多轮记忆。**checkpoint** 回答“这次运行进行到哪里了？”；它用于单次运行内的
崩溃恢复和幂等。两者都是追加式的：历史永不重写。

## Session

```python
from lovia import Runner, SQLiteSession

session = SQLiteSession("chat.db")

await Runner.run(agent, "我的项目叫 Atlas。", session=session, session_id="u1")
result = await Runner.run(agent, "我的项目叫什么？", session=session, session_id="u1")
# "Atlas"：第二次运行把第一次运行的 transcript 加载为历史
```

`session_id` 由你控制，可以按用户、线程、工单或产品里对“对话”的叫法来定。runner 会在运行
开始时加载之前的历史；当运行**完成**时，把本次运行自己的 entries 作为一个 `Segment`
追加进去（run id + entries + 不透明的每次运行 `meta`）。内置两个实现：
`SQLiteSession(path, *, wal=False)` 和 `InMemorySession()`。

### 契约

`Session` protocol 有四个方法：`segments(session_id)`、`load(session_id)`（扁平拼接）、
`append(session_id, entries, *, run_id=None, meta=None)`、`clear(session_id)`。
两个性质承担主要语义：

- **追加式。** 刻意没有 `replace`：已保存运行不可变。这让恢复运行可以安全重新加载历史，
  也让运行边界和每次运行 `meta`（例如携带的[压缩状态](context.md)）保持一致。
- **按 `run_id` 幂等。** 对已保存的 `run_id` 再次 append 不会做任何事，所以重放运行不会重复写入 entries。

中断的运行**不会**自动 append。它们存在 checkpoint 中。是否把一个被放弃的部分运行记录为已完成
segment，是**调用方**决定（内置 Web UI 在用户停止运行时会这样做）；finalize 部分运行时必须保持
工具一致性，`lovia.transcript.drop_dangling_tool_calls` 正是为此存在。

### 维护

长生命周期 session 会积累巨大的旧工具输出。内置存储（不是 protocol）提供了一个受控的
append-only 例外：

```python
trimmed = await session.trim_tool_results("u1", keep_chars=400, keep_runs=1)
```

它会截断最后 `keep_runs` 次运行之前的**已存**工具输出（结构、顺序和 `call_id` 配对保持不变；
操作幂等）。在依赖它之前，先在上下文策略上配一个
[`FileResultStore`](context.md#结果存储)：归档输出仍可通过 `recall_tool_result` 恢复；
没有归档的输出被截断后就真的没了。

## Checkpoint

需要扛住崩溃、或需要安全重发的运行，应该加 checkpoint：

```python
from uuid import uuid4

from lovia import CheckpointOptions, Runner, SQLiteCheckpointer

cp = SQLiteCheckpointer("runs.db")

result = await Runner.run(
    agent,
    "迁移报告格式。",
    checkpoint=CheckpointOptions(cp, f"report-migration-{uuid4().hex}"),
)
```

循环会在模型 turn 后、**每个工具结果**后保存 snapshot，所以崩溃最多丢失正在执行中的工作。
`RunSnapshot` 保存本次运行自己的 entries，再加一个小的可变 head（`RunHead`）：活跃 agent 名、
usage、turn count、状态（`running` / `interrupted` / `completed` / `failed`），以及上下文策略
携带的状态。你的 `context` 对象**不会**被 snapshot；恢复时需要重新传入。

### `run_id` 是幂等键

`run_id` 是 checkpoint 的**唯一、全局**键；和 session 不同，它没有作用域。所以它必须在一个
checkpointer 内唯一（UUID、job id 都可以）。id 已存在时怎么处理，由 `if_run_exists` 策略决定：

| 策略 | 已存在运行 | 没有已存运行 |
| --- | --- | --- |
| `"resume"`（默认） | 继续它（如果已完成则原样重放） | 开始新运行 |
| `"restart"` | 丢弃并重新开始 | 开始新运行 |
| `"fail"` | 抛 `UserError` | 开始新运行 |
| `"resume_only"` | 继续它 | 抛 `UserError` |

所以崩溃的 worker 只要重新发同一个调用：中断运行会恢复，已完成运行会重放保存结果并且不触碰模型，
两种情况下调用方都能拿到答案。两个细节：

- **恢复时新的 `input` 会被忽略**，因为 transcript 已经带着原始输入。`run_id` 是每次运行的
  幂等键，不是对话键；对话连续性请用 session。要按 id 继续一个已知运行且不带新输入：

  ```python
  Runner.run(agent, [], checkpoint=CheckpointOptions(cp, rid, if_run_exists="resume_only"))
  ```

- **重放只重新发终止事件。** hooks 和输出护栏已经在原始完成时运行过，不会再跑；usage 会折入
  调用方；`finish_reason` 为 `None`（没有持久化）。session 持久化会重新应用，但因为幂等，
  可以修复“checkpoint finalize 后、session append 前”崩溃留下的窗口。

`CheckpointOptions` 还接受 `delete_on_success=True`（运行完成后删除 snapshot，适合 durable 记录
已经在 session 里的运行）和 `resume_from=`（从你自己拿到的 `RunSnapshot` 恢复）。

### 恢复到底做了什么

恢复会重建运行：重新渲染 system prompt，重新加载 session 历史，追加 snapshot entries，然后
**处理未完成的工具调用**。也就是说，崩溃时留下但没有结果的工具调用会重新执行（同一个 turn
number），之后循环继续。已完成结果不会重跑；悬空调用会重跑。带副作用的工具请按“至少执行一次”的语义
设计，或让它们幂等。

恢复支持穿过 [handoff](multi-agent.md)：snapshot 按名称记录**活跃** agent，runner 会从入口
agent 的 handoff 图中重新解析。重命名或移除 agent 会让在途运行无法恢复（硬错误）；但已完成运行
的**重放**会降级为入口 agent 并记录 warning，已完成工作不能因为一次部署就报错。

## 两个存储的关系

```text
完整 transcript = session.load(session_id)   +   snapshot.entries
                  （所有已完成运行）             （唯一在途运行）
```

成功时顺序固定：先 finalize checkpoint，再 append session。因此一次运行不会同时处于
“已经作为完成结果持久化”和“仍然可恢复”的状态。两步之间的崩溃窗口会在下次重放时修复，因为
session append 是幂等的。

两个 SQLite 存储都接受 `wal=True`（默认关闭）：WAL journal mode 加 busy timeout，适合多个写入者
共享一个数据库文件，比如一个文件里有多个 store，或多进程部署。

## 容易踩的点

- **复用已完成的 `run_id` 会静默丢掉你的新输入**，这是设计（重放）。如果你想要“对话的下一轮”，
  那是 session，不是 checkpoint。
- **恢复会重新执行工具。** 只有已存结果的调用是安全的；悬空调用都会再跑。收费、发送等非幂等
  工具需要自己按 `ctx.run_id` + 调用参数去重。
- **跨 session 的 `run_id` 碰撞不会破坏数据，但会让一切混乱。** checkpointer 是全局的；
  两个“运行”共用 id，对它来说就是同一个运行。生成 id，不要从用户输入拼。
- **`InMemory*` 存储无法跨进程存活。** 它们用于测试和临时聊天；把
  `InMemoryCheckpointer` 和“崩溃恢复”配在一起用法就不对。

## 延伸阅读

- [核心概念：Session vs checkpoint](concepts.md#session-vs-checkpoint)：心智模型
- [上下文管理](context.md)：携带状态和结果存储
- [Web UI 与服务端](web.md)：内置服务端如何接 session
- 示例：[`05_sessions.py`](../../examples/05_sessions.py)，
  [`15_resume.py`](../../examples/15_resume.py)
