# Session 与 Checkpoint

两类持久化需求分别由两种存储解决。**Session** 记录“这段对话到目前为止说过什么”，
用于保存跨运行的多轮对话；**Checkpoint** 记录“本次运行执行到哪里了”，用于单次运行的
崩溃恢复和幂等处理。Runner 正常运行时只会追加记录，不会覆写已有历史；内置存储另有明确的
维护操作，可以按需截断或回退 Session。

## Session

```python
from lovia import Runner, SQLiteSession

session = SQLiteSession("chat.db")

await Runner.run(agent, "我的项目叫 Atlas。", session=session, session_id="u1")
result = await Runner.run(agent, "我的项目叫什么？", session=session, session_id="u1")
# "Atlas"：第二次运行把第一次运行的 transcript 加载为历史
```

`session_id` 由调用方指定，可以按用户、线程、工单或产品中的其他“对话”实体来划分。
Runner 在运行开始时加载历史记录；运行**完成**后，将本次运行产生的 entries 作为一个
`Segment` 追加进去，其中包含 run id、entries 和不透明的单次运行 `meta`。内置两个实现：
`SQLiteSession(path, *, wal=False)` 和 `InMemorySession()`。

### 契约

`Session` protocol 有四个方法：`segments(session_id)`、`load(session_id)`（扁平拼接）、
`append(session_id, entries, *, run_id=None, meta=None)`、`clear(session_id)`。
主要语义来自两个性质：

- **追加式。** 不提供 `replace`：已保存运行不可变。这让恢复运行可以安全重新加载历史，
  也让运行边界和每次运行 `meta`（例如携带的[压缩状态](context.md)）保持一致。
- **按 `run_id` 幂等。** 对已保存的 `run_id` 再次 append 不会做任何事，所以重放运行不会重复写入 entries。

中断的运行**不会**自动写入 Session，而是保留在 checkpoint 中。是否将放弃的部分结果作为
已完成 segment 写入，由**调用方**决定；内置 Web UI 会在用户停止运行时这样处理。写入前必须
移除没有对应结果的工具调用，保持 transcript 完整，可使用
`lovia.transcript.drop_dangling_tool_calls` 处理。

### 维护

除 `Session` protocol 已定义的 `clear()` 外，内置存储还允许在两种维护场景下修改已有数据。
下面两项能力不属于 protocol，自定义存储无需实现。

长期使用的 session 可能积累大量旧工具输出。可用 `trim_tool_results` 回收空间：

```python
trimmed = await session.trim_tool_results("u1", keep_chars=400, keep_runs=1)
```

它会截断最后 `keep_runs` 次运行之前的**已存**工具输出（结构、顺序和 `call_id` 配对保持不变；
操作幂等）。在依赖它之前，先在上下文策略上配一个
[`FileResultStore`](context.md#结果存储)：归档输出仍可通过 `recall_tool_result` 恢复；
没有归档的输出被截断后就无法恢复。

`rewind` 从指定位置截去 transcript 尾部，是“编辑后重发”和“重新生成”的底层操作。
它会直接修改现有历史，不会创建分支：

```python
removed = await session.rewind("u1", keep_entries=12)
```

`keep_entries` 按 `load()` 返回的扁平视图计数。切点之后的运行会被整段删除；如果切点
落在某次运行内部，该运行会被截断，同时移除切点处失去结果的工具调用，以保证记录仍然
完整。被截断运行的 `meta` 也会清除，因为其中的上下文状态是在运行结束时计算的，已与
回退后的 transcript 不一致。`keep_entries=0` 会清空 session；切点位于末尾或超过末尾时
不做任何操作。若被回退的运行仍留有 checkpoint，也应一并删除，否则后续恢复会重新写入
已经撤销的尾部内容。

## Checkpoint

如果运行需要在进程崩溃后恢复，或需要安全地重复提交，应启用 checkpoint：

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

循环会在模型轮次后、**每个工具结果**后保存 snapshot，所以崩溃最多丢失正在执行中的工作。
`RunSnapshot` 保存本次运行自己的 entries，再加一个小的可变 head（`RunHead`）：活跃 agent 名、
usage、轮次计数、状态（`running` / `interrupted` / `completed` / `failed`），以及上下文策略
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

所以崩溃的 worker 只要重新提交同一个调用：中断运行会恢复，已完成运行会重放保存结果并且不触碰模型，
两种情况下调用方都能拿到答案。有两个细节需要注意：

- **恢复时新的 `input` 会被忽略**，因为 transcript 已经带着原始输入。`run_id` 是每次运行的
  幂等键，不是对话键；对话连续性请用 session。要按 id 继续一个已知运行且不带新输入：

  ```python
  Runner.run(agent, [], checkpoint=CheckpointOptions(cp, rid, if_run_exists="resume_only"))
  ```

- **重放只会再次发送终止事件。** 钩子和输出护栏已在原运行结束时执行，不会重复运行；用量会计入
  调用方；`finish_reason` 为 `None`（没有持久化）。session 持久化会重新应用，但因为幂等，
  可以修复“checkpoint 完成后、session 追加前”崩溃留下的窗口。

`CheckpointOptions` 还接受 `delete_on_success=True`（运行完成后删除 snapshot，适合持久记录
已经在 session 里的运行）和 `resume_from=`（从你自己拿到的 `RunSnapshot` 恢复）。

### 恢复运行的过程

恢复会重建运行：重新渲染 system prompt，重新加载 session 历史，追加 snapshot entries，然后
**处理未完成的工具调用**。也就是说，崩溃时留下但没有结果的工具调用会重新执行（同一个轮次编号），
之后循环继续。已完成结果不会重跑；悬空调用会重跑。带副作用的工具请按“至少执行一次”的语义
设计，或让它们幂等。

恢复支持穿过 [handoff](multi-agent.md)：snapshot 按名称记录**活跃** agent，runner 会从入口
agent 的 handoff 图中重新解析。重命名或移除 agent 会让在途运行无法恢复（硬错误）；但已完成运行
的**重放**会降级为入口 agent 并记录 warning 日志，已完成工作不会因为一次部署就报错。

## 两个存储的关系

```text
完整 transcript = session.load(session_id)   +   snapshot.entries
                  （所有已完成运行）             （唯一在途运行）
```

成功时顺序固定：先完成 checkpoint，再追加 session。因此一次运行不会同时处于
“已经作为完成结果持久化”和“仍然可恢复”的状态。两步之间的崩溃窗口会在下次重放时修复，因为
session append 是幂等的。

两个 SQLite 存储都接受 `wal=True`（默认关闭）：WAL journal mode 加上 busy timeout，适合多个写入者
共享一个数据库文件，比如一个文件里有多个 store，或多进程部署。

## 注意事项

- **复用已完成的 `run_id` 会忽略你的新输入**，这是设计（重放）。如果你想要“对话的下一轮”，
  那是 session，不是 checkpoint。
- **恢复会重新执行工具。** 只有已存结果的调用是安全的；悬空调用都会再跑。收费、发送等非幂等
  工具需要自己按 `ctx.run_id` + 调用参数去重。
- **跨 session 的 `run_id` 碰撞不会破坏数据，但会让语义混乱。** checkpointer 是全局的；
  两个“运行”共用 id，对它来说就是同一个运行。请生成 id，不要从用户输入拼。
- **`InMemory*` 存储无法跨进程存活。** 它们用于测试和临时聊天；把
  `InMemoryCheckpointer` 和“崩溃恢复”配在一起就不合适。

## 延伸阅读

- [核心概念：Session 与 Checkpoint](concepts.md#session-与-checkpoint)：关键区别
- [上下文管理](context.md)：携带状态和结果存储
- [Web 服务端](web-server.md)：内置服务端如何接入 Session
- 示例：[`05_sessions.py`](../../examples/05_sessions.py)，
  [`15_resume.py`](../../examples/15_resume.py)
