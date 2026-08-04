# 记忆

Session 保存当前对话，`Memory` 则让信息跨对话保留。它分为两层：

- **Notes**（热层）：一小块有字符预算的长期事实，**始终注入**系统提示词。默认会在运行结束后
  自动提炼，并定期清理；也可以通过 `remember(fact)` / `forget(fact)` 手动维护。
- **Archive**（冷层）：可全文搜索的过往对话存储，只在需要时用 `recall(query)` 拉进来。

如果信息只需留在当前对话，Session 已经足够；只有需要跨对话复用时才启用 Memory。
不希望保留可搜索的对话归档时，使用 `Memory(..., index=None)`，只保留 Notes。

```python
from lovia import Agent, Memory

agent = Agent(
    name="assistant",
    model="<model>",
    plugins=[Memory("./.lovia/memory")],
)
```

默认配置使用标准库 SQLite 全文搜索，无须额外服务或嵌入模型。检索前，模型会自动补充同义词和
翻译，减少措辞不同造成的漏检；后文还介绍了向量检索和自定义索引。

## 存储结构

```text
.lovia/memory/
├── MEMORY.md      # 热层：每行一个 `- [YYYY-MM] 事实`，可直接编辑
├── MEMORY.md.bak  # 上一次实际重写前的 Notes，可撤销一层
├── .dreamed       # 周期标记，用于判断何时再次整理
├── archive.db     # 冷层：过往对话的关键词索引
└── vectors.db     # 冷层：向量检索分支（只有 embedder= 时存在）
```

`MEMORY.md` 可直接编辑。写入采用原子替换，读取时会忽略不以 `- ` 开头的行。其他文件均按需
创建；首次启用时不一定全部存在。

> **隐私。** Archive 会把用户消息和助手回复持久化到磁盘。请为记忆目录设置合适的
> 访问控制；如果不想保留可搜索的对话记录，传 `index=None`，只保留 Notes，`recall`
> 工具也会消失。

## 记忆如何写入

三条路径，从自动到手动：

1. **运行后提炼**（`auto_curate=True`，默认）。每次运行结束（`RunCompleted`）时，会根据完整
   Transcript 做一次摘要提炼（digest）。它只把少量稳定、以后仍有用的事实写入 Notes，并加上
   `[YYYY-MM]` 日期；可以从文件或命令重新获取的状态、一次性任务细节等不会写入，因此多数
   运行不会新增 Notes。如果本次对话证明某条旧记录已经错误或过时，提炼时也会将其删除。
   同时，它会把一份脱离当前上下文仍能读懂的对话摘要写入 Archive，方便以后检索。
   原始用户消息和助手回复也会建立索引；文档 ID 固定为 `run_id:seq`，所以重放运行只会
   更新原记录，不会重复写入。
2. **模型在运行中写入**：`remember` / `forget` 工具，只用于用户明确要求记住或纠错的场合；
   其他长期信息交给运行结束时的提炼步骤判断。一次性任务细节不进入 Notes。
3. **你的代码写入**：同样的动词也是公开方法，不需要模型参与：

   ```python
   mem = Memory("./memory")
   await mem.remember("偏好用中文简洁回答。")
   await mem.forget("旧偏好")
   body = await mem.notes_body()          # 编辑器读取
   await mem.replace_notes(edited_body)   # 编辑器写入（规范化 + 去重）
   ```

   `GET` / `PUT /api/memory` 也基于最后这一对方法。

Archive 对原始消息的建索引不依赖 `auto_curate`。将它设为 `False` 只会关闭运行后的事实提炼和
对话摘要生成；只要 `index` 没有设为 `None`，原始用户消息和助手回复仍会进入 Archive。

## 定期整理

Notes 长期追加后，容易积累重复说法、互相矛盾的旧记录和已经失效的细节。为此，插件会
定期调用 `dream` 整理整个列表：合并含义相同的条目、拆开挤在一行里的不同事实，并根据
`[YYYY-MM]` 日期保留较新的说法。一次性事件、任务产物、可随时重新获取的状态和过期内容
会被清理。整理过程需要一次模型调用；提示词会明确要求模型保留用户的规则和偏好，不得补写
原列表中没有的事实。

有三种触发方式：

- **按周期**：每次运行结束时检查。`.dreamed` 记录的基准时间过去
  `dream_every_days` 天（默认 3）后，如果 `MEMORY.md` 有过变化，就会执行整理。未超出预算时，
  首次检查只创建 `.dreamed` 标记并开始计时，不会立刻调用模型。
- **超出预算**：运行结束时，如果 Notes 已超过 `notes_budget`（默认 8000 字符），无需等待
  周期到期便会整理。
- **手动执行**：调用 `await mem.dream(model=...)`，返回整理前后的条目数
  `(before, after)`；如果创建
  `Memory` 时已经指定 `model=`，这里可以省略。

只要模型实际返回了可用的重写结果，插件就会先把原 Notes 保存到 `MEMORY.md.bak`，因此可以
撤销一层。这个备份不会保留更多版本；如需多级回滚，请自行使用版本控制。Archive 保存的是
可搜索的历史对话，并不是 Notes 的版本库。

默认情况下，运行结束后的 Memory 维护会同步完成：`Runner.run` 返回时，提炼和到期的定期整理
都已处理完毕。长生命周期的服务可以设置 `curate_in_background=True`，避免模型调用拖延
运行的最终事件。关闭服务前再 `await mem.drain()`，等待仍在后台执行的维护任务。内置 Web 服务采用的
就是这种方式，关闭时最多等待 15 秒。

## 提升召回质量

```python
Memory("./memory")                             # 标准库关键词检索（FTS5 bm25）
Memory("./memory", embedder=OpenAIEmbedder())  # + 语义检索分支 → 混合召回
Memory("./memory", index=my_index)             # 带上自己的检索引擎
```

默认索引使用 SQLite FTS5，在适配中日韩文本的二元分词索引上执行 BM25 检索。搜索前，
`recall` 会用模型补充同义词和翻译；`expand_query="auto"` 只在默认的纯关键词索引中启用。
命中后，默认返回模型归纳的结果，而不是原始摘录（`summarize_recall=True`）。查询扩展失败时
仍会搜索原始问题，归纳失败时则直接返回原始命中，不会让整个 `recall` 调用失败。

**`embedder=`** 会把默认索引升级为关键词与向量混合检索，并用 Reciprocal Rank Fusion（RRF）
合并两路结果。向量仍存放在 SQLite 中，无须另建服务；`OpenAIEmbedder` 可以连接任意兼容
OpenAI `/embeddings` 接口的端点：

```python
OpenAIEmbedder(model="text-embedding-3-small", dimensions=None, batch_size=32)
```

聊天模型和嵌入模型常常部署在不同端点，因此 Embedder 会优先读取
`LOVIA_EMBEDDING_BASE_URL` / `LOVIA_EMBEDDING_API_KEY`，未设置时再回退到聊天端点的
`OPENAI_BASE_URL` / `OPENAI_API_KEY`。不同 Embedder 生成的向量会按 ID 分区存储；
ID 改变后，旧缓存会清空并重新建立，不会混用不同的向量空间。

**`index=`** 完全替换检索层。`Index` 围绕普通文档提供三个方法：`add` / `remove` /
`search`，相同 `Doc.id` 会更新原文档。你可以基于 Elasticsearch、pgvector 或其他检索系统实现：

```python
class Index(Protocol):
    async def add(self, docs: list[Doc]) -> None: ...
    async def remove(self, ids: list[str]) -> None: ...
    async def search(self, query: str, k: int = 5) -> list[Hit]: ...
```

多个检索分支可以用 `|` 组合：`KeywordIndex(...) | VectorIndex(...) | my_index` 会得到一个
由 RRF 融合的 `HybridIndex`。查询时，失败的分支会被跳过；写入则会发送到所有分支。
自定义 Index 混入 `Fusable` 后也可以使用 `|` 运算符。（`embedder=` 和 `index=` 互斥；
前者只是用来快速构建默认混合索引。）

热层同样可以替换。`NotesStore` 只有 `load` 和 `save` 两个方法，负责读取和保存事实列表；
规范化、去重和预算策略都由插件处理，因此 Redis 或数据库后端通常只需少量代码。
写入 `MEMORY.md` 的 `FileNotesStore` 是参考实现。

## 配置参考

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `root` | `./.lovia/memory` | 默认存储位置（显式传入某层后端时，该层忽略它） |
| `notes` | `None` → `MEMORY.md` 文件存储 | 热层后端 |
| `index` | 默认关键词索引 | 冷层后端；`None` 关闭这一层和 `recall` 工具 |
| `embedder` | `None` | 给默认索引加向量分支 |
| `auto_curate` | `True` | 运行结束时提取长期事实、删除已失效的 Notes，并将对话摘要写入 Archive |
| `curate_in_background` | `False` | 在后台执行运行后维护，不阻塞运行完成；关闭前配合 `drain()` 使用 |
| `expand_query` | `"auto"` | 用模型扩展检索词；`auto` 只为默认的纯关键词索引启用 |
| `summarize_recall` | `True` | 让 `recall` 返回模型归纳后的命中结果 |
| `recall_k` | `5` | 每次 `recall` 取回的结果数 |
| `notes_budget` | `8000` | Notes 字符预算；超出后会在运行结束时触发整理 |
| `dream_every_days` | `3` | 定期整理的间隔；`None` 表示只在超出预算时触发 |
| `model` | 宿主 Agent 的模型 | 运行后提炼、定期整理和召回辅助查询使用的模型 |

这些模型调用会复用 `Runner.run`，但使用一个没有工具和插件、temperature 为 0 的子 Agent，
因此可以沿用现有 Provider，又不会递归触发 Memory。lovia 会持久保存完整 Transcript，
而且[压缩只影响模型看到的视图](context.md)，所以运行后提炼始终基于本次运行的完整记录。

## 使用建议

- **自定义后端会在多个运行间共享，也可能被并发访问。** 实现必须保证并发安全，其生命周期
  由创建者管理，插件不会主动关闭。Notes 的“读取—修改—写入”由内部锁串行化，内置 SQLite
  存储也会自行串行化。
- **启用 `auto_curate` 后，每次完成运行都会增加一次提炼模型调用。** 定期整理到期或超出预算时，
  还会再调用一次。高流量、低价值场景可以设置 `auto_curate=False`，依赖 `remember` 工具，
  或把 `model=` 指向更便宜的模型。关闭运行后提炼不会关闭定期整理，也不会关闭超预算整理。
- **一次 `recall` 最多会增加两次辅助模型调用。** 默认关键词索引会扩展查询，命中结果还会经过
  归纳；分别设置 `expand_query=False` 和 `summarize_recall=False` 可以关闭。
- **定期整理的状态文件始终放在 `root` 下。** `.dreamed` 周期标记和 `MEMORY.md.bak` 都是普通文件；
  即使 `notes=` 使用自定义存储也一样，此时备份保存的是存储内容的文本导出。自定义存储没有文件
  修改时间可供比较，因此周期到期后，即使内容未变也会再次整理。
- **后台维护采用尽力而为的语义。** 进程不调用 `drain()` 就退出，可能丢失最后一次运行的维护结果。
  Transcript 仍会保留在 Session 中，但如果应用要求每次都完成 Memory 写入，关闭流程必须调用
  `drain()`。
- **`recall` 的效果取决于已有归档。** 之前用 `index=None` 的对话，后来就是搜不到；
  Archive 从启用索引的那一刻才开始积累。

## 延伸阅读

- [插件](plugins.md)：Memory 是跨运行状态插件的代表
- [Session 与 Checkpoint](sessions-and-checkpoints.md)：对话内持久化，以及 Transcript 从哪里来
- [Web UI](web-ui.md)：内置 Memory 编辑器
- 示例：[`23_memory.py`](../../examples/23_memory.py)
