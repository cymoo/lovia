# 记忆

Session 让 Agent 记住**当前对话**的历史，但信息不会自动跨对话传递，用户每次都要
重新说明自己的偏好。`Memory` 插件提供长期记忆，采用模型容易理解的**两层结构、三个动作**：

- **Notes**（热层）：一小块有字符预算的持久事实，**始终注入** system prompt。通过
  `remember(fact)` / `forget(fact)` 维护。
- **Archive**（冷层）：可全文搜索的过往对话存储，只在需要时用 `recall(query)` 拉进来。

```python
from lovia import Agent, Memory

agent = Agent(
    name="assistant",
    model="<model>",
    plugins=[Memory("./.lovia/memory")],
)
```

这一方案开箱即用：基于标准库 SQLite 的全文搜索，无须额外服务，也无须嵌入模型（embedding）。
大模型能够弥合不同措辞之间的差异，因此召回效果依然可靠（见下文）。

## 存储结构

```text
.lovia/memory/
├── MEMORY.md      # 热层：每行一个 `- fact`，人可以直接编辑
├── archive.db     # 冷层：过往对话的关键词索引
└── vectors.db     # 冷层：向量检索分支（只有 embedder= 时存在）
```

`MEMORY.md` 有意保持普通 markdown：用编辑器打开，删一行，就完成了。写入是原子的；
读取时非 bullet 行会被忽略。

> **隐私。** Archive 会把用户和 assistant 消息文本持久化到磁盘。请对 memory 目录设置合适的
> 访问控制；如果不想保留可搜索的对话记录，传 `index=None`，只保留 Notes，`recall`
> 工具也会消失。

## 记忆如何写入

三条路径，从自动到手动：

1. **自动整理**（`auto_curate=True`，默认）。每次运行结束（`RunCompleted`）时，会对完整
   transcript 做一次 digest 调用，把少量持久事实提升到 Notes，并把自包含的 episode
   summary 写入 archive。summary 比原始聊天片段更容易搜索。原始用户/assistant 消息也会建索引；
   document id 是确定性的（`run_id:seq`），所以重放运行会 upsert，而不是重复写入。
2. **模型在运行中写入**：`remember` / `forget` 工具，并由注入的 instructions 引导
   （“主动保存持久事实”）。
3. **你的代码写入**：同样的动词也是公开方法，不需要模型参与：

   ```python
   mem = Memory("./memory")
   await mem.remember("偏好用中文简洁回答。")
   await mem.forget("旧偏好")
   body = await mem.notes_body()          # 编辑器读取
   await mem.replace_notes(edited_body)   # 编辑器写入（规范化 + 去重）
   ```

   Web UI 侧边栏的 Memory 编辑器（`GET`/`PUT /api/memory`）就是基于最后这一对方法。

Notes 会保持在 `notes_budget` 内（默认 5000 字符，模型会看到一个 meter）。digest 后如果超预算，
会再做一次 consolidation 调用，把列表合并重写到预算内。

默认 curation **内联**运行：`Runner.run` 返回时，memory 已经整理完。长生命周期宿主可以传
`curate_in_background=True`，这样运行的最终事件不会被 curation 的模型调用阻塞；关闭时 await
`mem.drain()`，完成在途整理的收尾（内置 web server 正是这样做的，带 15s 上限）。

## 提升召回质量

```python
Memory("./memory")                             # 标准库关键词检索（FTS5 bm25）
Memory("./memory", embedder=OpenAIEmbedder())  # + 语义检索分支 → 混合召回
Memory("./memory", index=my_index)             # 带上自己的检索引擎
```

**零配置**使用 SQLite FTS5：在 CJK-aware bigram 索引上做 bm25。LLM 会补足关键词检索错过的内容：
`recall` 查询会在搜索前扩展同义词和翻译（`expand_query="auto"` 只在默认纯词面索引时开启），
命中结果会作为模型写出的 summary 返回，而不是原始摘录（`summarize_recall=True`）。这两个 LLM
辅助步骤失败时都会放行：扩展或总结出错时，会退化到原始 query / 原始命中。

**`embedder=`** 会把默认索引升级成关键词 | 向量混合检索，并用 Reciprocal Rank Fusion 合并：
无需新服务（向量也在 SQLite 里），就能获得语义和跨语言召回。`OpenAIEmbedder` 可以对接任意
OpenAI 兼容 `/embeddings` 端点：

```python
OpenAIEmbedder(model="text-embedding-3-small", dimensions=None, batch_size=32)
```

聊天模型和嵌入模型服务常常部署在不同端点，因此 embedder 会优先读取
`LOVIA_EMBEDDING_BASE_URL` / `LOVIA_EMBEDDING_API_KEY`，未设置时再回退到聊天端点的
`OPENAI_BASE_URL` / `OPENAI_API_KEY`。不同 embedder 生成的向量会按 id 分区存储；
id 改变后，旧缓存会清空并重新建立，不会混用不同的向量空间。

**`index=`** 完全替换检索层。`Index` 围绕普通文档提供三个方法：`add` / `remove` /
`search`，按 `Doc.id` upsert。你可以基于 Elasticsearch、pgvector 或任何系统实现：

```python
class Index(Protocol):
    async def add(self, docs: list[Doc]) -> None: ...
    async def remove(self, ids: list[str]) -> None: ...
    async def search(self, query: str, k: int = 5) -> list[Hit]: ...
```

检索分支可以用 `|` 组合：`KeywordIndex(...) | VectorIndex(...) | my_index` 会得到一个
RRF 融合的 `HybridIndex`。读取失败时会放行（坏掉的分支会跳过），写入发往每个分支；任何 index
只要 mix in `Fusable` 就能获得 `|` 运算符。（`embedder=` 和 `index=` 互斥；embedder 只是构建
默认 hybrid 的语法糖。）

热层同样可替换：`NotesStore` 只有两个方法（`load`/`save` fact list）。规范化、去重和预算策略
都留在插件里，所以 Redis 或 DB 后端 store 只需十几行。`FileNotesStore`，也就是
`MEMORY.md` 写入器，是参考实现。

## 配置参考

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `root` | `./.lovia/memory` | 默认存储位置（显式传入某层后端时，该层忽略它） |
| `notes` | `None` → `MEMORY.md` 文件存储 | 热层后端 |
| `index` | 默认关键词索引 | 冷层后端；`None` 关闭这一层和 `recall` 工具 |
| `embedder` | `None` | 给默认索引加向量分支 |
| `auto_curate` | `True` | 运行结束 digest：facts → Notes，episode summary → archive；Notes 超预算时 consolidation |
| `curate_in_background` | `False` | 不让 curation 阻塞运行完成；配合 `drain()` 使用 |
| `expand_query` | `"auto"` | LLM 查询扩展；auto = 只在默认纯词面索引时启用 |
| `summarize_recall` | `True` | `recall` 返回模型写出的命中 summary |
| `recall_k` | `5` | 每次 recall 取回的命中数 |
| `notes_budget` | `5000` | Notes 字符预算，也是 prompt meter 和 consolidation 触发点 |
| `model` | 宿主 agent 的模型 | curation/recall 辅助查询使用的模型 |

辅助查询（digest、consolidation、query expansion、summarization）会复用 `Runner.run`：
用一个没有工具、没有插件、temperature 0 的子 agent，因此复用你的 provider 链，且**不会递归**
（子 agent 没有 Memory 插件）。因为 lovia 的 transcript 是持久的，而且
[压缩只作用于 view](context.md)，digest 会在**完整** transcript 上执行一次：它负责整理，不负责补救。

## 注意事项

- **自定义后端会被每个运行共享**，可能并发访问。它们必须并发安全，插件也不会关闭它们；
  生命周期属于创建者。（Notes read-modify-write 由内部锁串行化；SQLite 存储内部也会串行化。）
- **curation 每次运行要花一次模型调用**（Notes 超预算时是两次）。高流量、低价值场景可以设置
  `auto_curate=False`，依赖 `remember` 工具，或把 `model=` 指向更便宜的模型。
- **后台 curation 是尽力而为。** 进程不 `drain()` 就退出，可能丢失最后一次运行的整理结果。
  这是有意接受的折中（transcript 仍在 session 里），但如果你期待强持久性，就需要特别注意。
- **`recall` 的效果取决于已有归档。** 之前用 `index=None` 的对话，后来就是搜不到；
  archive 从你开启它那一刻才开始。

## 延伸阅读

- [插件](plugins.md)：Memory 是跨运行状态插件的代表
- [Session 与 Checkpoint](sessions-and-checkpoints.md)：对话内持久化，以及 transcript 从哪里来
- [Web UI](web-ui.md)：内置 Memory 编辑器
- 示例：[`23_memory.py`](../../examples/23_memory.py)
