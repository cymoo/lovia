# 记忆

Session 保存当前对话，`Memory` 则让信息跨对话保留。它分为两层：

- **Notes**（热层）：一小块有字符预算的长期事实，**始终注入**系统提示词（按会话固定，
  不破坏提示词缓存）。由 Agent 自己用 `remember(fact)` / `forget(fact)` 写入。
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
├── archive.db     # 冷层：过往对话的关键词索引
├── vectors.db     # 冷层：向量检索分支（只有 embedder= 时存在）
└── scopes/        # 只有 scope= 时存在：每个作用域键一个目录
    └── lovia-3f9a1c2e/  # 键的末级名 + 键的短哈希
        ├── scope.json   # 原始键
        ├── MEMORY.md    # 该作用域的 Notes
        └── archive.db   # 该作用域的过往对话（以及 vectors.db）
```

`MEMORY.md` 可直接编辑。写入采用原子替换，读取时会忽略不以 `- ` 开头的行。其他文件均按需
创建；首次启用时不一定全部存在。

> **隐私。** Archive 会把用户消息和助手回复持久化到磁盘。请为记忆目录设置合适的
> 访问控制；如果不想保留可搜索的对话记录，传 `index=None`，只保留 Notes，`recall`
> 工具也会消失。

## 记忆如何写入

**Notes 只有一个主动写入者：Agent 自己。** 运行结束后不会有任何步骤从对话里提炼事实；
一条记录之所以存在，是因为用户说出值得保留的话时，Agent 当场调用了 `remember`。只有这个
时刻能看到完整对话，分得清“以后都这样”和“这次这样”。

什么值得记写在工具描述里：只收四类，且只取用户说过或确认过的内容。

- 用户希望 Agent 怎么做事：一次纠正、一条长期偏好或规矩；
- 用户是谁：角色和长期情况；
- 东西在哪里：服务器、账号、工具等，不记下来用户就得再说一遍的外部资源；
- 用户工作中的决定和约束，而且文件和历史里看不出来。

其余内容各有归处，不进 Notes：项目文件、说明文件和版本历史里已有的；手头任务的进度和结果；
Agent 自己的发现和结论；对用户兴趣或情绪的猜测；只针对这一次的要求；随手能查到的通用知识。
拿不准就不记：漏记只需用户再说一遍，错记会误导之后的每个会话。用户明确说“记住这个”时
总是照办。

Notes 靠写入方式保持精简，不靠事后清理：

- **取代旧记录的事实会替换它**：`remember(fact, replaces=<旧记录>)` 一次写入完成替换，
  纠正不会和被纠正的内容并排留着。
- **写满了就拒绝新增**：超过 `notes_budget`（默认每层 4000 字符，Agent 能看到用量）后，
  `remember` 会返回当前的 Notes，要求先腾出位置（替换一条，或 `forget` 一条）再保存。
- 新记录带 `[YYYY-MM]` 日期，Agent 和你都能看出一条记录有多旧。

Archive 不需要判断：每次运行结束（`RunCompleted`）时，这次运行会作为**一篇文档**建立索引，
内容是其中的用户消息和助手回复，按顺序排列，问题和回答挨在一起。文档 ID 就是运行 ID，
所以恢复后的运行只会更新原记录，不会重复写入。工具调用和结果不进索引；特别长的运行只保留
开头和结尾。整个过程不调用模型。

你的代码也可以写入。同样的动词也是公开方法，不需要模型参与：

```python
mem = Memory("./memory")
await mem.remember("偏好用中文简洁回答。")
await mem.remember("用 uv 部署。", replaces="用 pip 部署。")
await mem.forget("旧偏好")
body = await mem.notes_body()          # 编辑器读取
await mem.replace_notes(edited_body)   # 编辑器写入（规范化 + 去重）
```

`GET` / `PUT /api/memory` 基于最后这一对方法。代码写入不受 `notes_budget` 限制。

### 自己提炼事实

有些场景确实需要从每次运行里提取事实，例如新用户引导，或者必须记下客户信息的客服。这只需要
一个 `RunCompleted` 钩子加 `mem.remember()`，用什么模型、按什么标准提取都由你决定：

```python
from lovia import Agent, AgentHooks, Memory
from lovia.events import RunCompleted

mem = Memory("./memory")
hooks = AgentHooks()

@hooks.on(RunCompleted)
async def extract(ev: RunCompleted, ctx) -> None:
    for fact in await my_extractor(ev.result.entries):   # 你自己的模型调用
        await mem.remember(fact)

agent = Agent(name="assistant", model="<model>", plugins=[mem], hooks=hooks)
```

## Notes 与提示词缓存

Notes 位于系统提示词中，而模型服务商缓存的是提示词前缀：先是系统提示词，再是整段对话历史。
如果对话中途改动 Notes，下一次运行就要为整段历史重新付费。因此每个会话会**固定**使用它开始时的
那份 Notes：期间记下的事实会立即写入 `MEMORY.md`，本身也已经出现在对话里，
系统提示词则保持逐字节不变。

会话闲置超过 `notes_refresh_after` 秒（默认 300，即 Anthropic 默认的缓存时长）后，下一次运行
会重新读取 Notes；此时服务商缓存本来就已过期，刷新不带来额外开销。缓存时长更长时可以调大；
设为 `0` 则每次运行都读取最新 Notes。没有 `session_id` 的运行总是读取最新 Notes；
`replace_notes()` 和 `clear_scope()` 属于主动编辑，会让所有会话的快照失效。快照只保存在进程
内存中，进程重启后会重新读取。

## 作用域

只在一个地方工作的助手，一份 Notes 就够了。跨多个项目工作的 Agent 需要两类记忆：关于
**用户**的事实（在哪里都成立），和关于**这个项目**的事实（它的决定、过往对话）。
`scope=` 把每次运行映射到一个键，每个键在用户那一层旁边有自己的 Notes 和 Archive：

```python
Memory(
    "~/.lovia/memory",
    scope=lambda ctx: ctx.deps.project_path,  # (RunContext) -> str | None
    scope_name="project",                     # 提示词里怎么称呼这个作用域
)
```

这个函数在每次运行开始时调用一次，之后整次运行（工具、提示词、归档）都落在同一个
作用域里。返回 `None` 表示这次运行不属于任何作用域，行为与不传 `scope=` 的 `Memory` 完全相同。
属于作用域的运行：

| | 不属于作用域的运行 | 属于作用域的运行 |
| --- | --- | --- |
| 提示词 | `NOTES` | `USER NOTES` + `PROJECT NOTES`，各有自己的计量和完整的 `notes_budget` |
| `remember` | `remember(fact)` | `remember(fact, about)`，`about` 取 `"user"` 或 `scope_name` |
| `replaces` / `forget` | 用户的 Notes | 两层都找（最接近的匹配胜出） |
| `recall` | 顶层 Archive | 只搜该作用域的 Archive |
| 运行结束 | 这次运行 → 顶层 Archive | 这次运行 → 该作用域的 Archive |

一条事实归哪一层由模型判断，判据只有一条：说的是这个人（偏好、长期规矩、长期情况）→ 用户；
说的是这个项目或这件工作 → 作用域；拿不准时归作用域——项目事实误放到全局，会串到所有
别的项目里；偏好误放在项目里，代价只是换个项目再说一遍。

所有读写某一层的公开方法都接受 `scope=`：`None`（默认）是用户层，传键就是该作用域。
`clear_scope(key)` 会同时删除一个作用域的 Notes **和** Archive——Archive 没有别的途径清除：

```python
await mem.remember("部署走 `make ship`。", scope="/srv/app")
body = await mem.notes_body(scope="/srv/app")
await mem.replace_notes(edited, scope="/srv/app")
await mem.clear_scope("/srv/app")          # 不可撤销
```

任何字符串都可以作为键（`/srv/app`、`ssh://box/x`）：目录名是键的末级名加短哈希，
`scope.json` 记下原始键。作用域只使用默认存储，所以 `scope=` 不能与 `notes=` 或自定义
`index=` 同时传；`index=None` 和 `embedder=` 对每一层都生效。

用 `lovia.web` 提供服务时，`scope=chat_project` 按对话所在的目录分层，`lovia web` 就是这样做的。
键是对话的工作区（解析后的路径，或 `ssh://…` / `docker://…`）；在 `WorkspaceSwitch(general=)`
那个兜底目录（侧栏的**对话**分组）里的对话、没有工作区的对话，都不属于任何作用域。设置 → 记忆里的"项目"范围
只对用它分层的 `Memory` 出现：

```python
from lovia.web import chat_project

Memory("~/.lovia/memory", scope=chat_project, scope_name="project")
```

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
| `scope` | `None` | `(RunContext) -> str \| None`：按每次运行的键拆分记忆（见[作用域](#作用域)） |
| `scope_name` | `"project"` | 提示词里对作用域的称呼，也是 `about` 对应的取值 |
| `expand_query` | `"auto"` | 用模型扩展检索词；`auto` 只为默认的纯关键词索引启用 |
| `summarize_recall` | `True` | 让 `recall` 返回模型归纳后的命中结果 |
| `recall_k` | `5` | 每次 `recall` 取回的结果数 |
| `notes_budget` | `4000` | 每层 Notes 的字符预算：提示词里的用量计，超出后 `remember` 拒绝新增 |
| `notes_refresh_after` | `300` | 会话最后一次运行后保留 Notes 快照的秒数；`0` 表示每次运行都重新读取 |
| `model` | 宿主 Agent 的模型 | `recall` 的辅助查询使用的模型 |

Memory 只在 Agent 调用 `recall` 时才调用模型。这些辅助查询（扩展、归纳）复用 `Runner.run`，
但使用一个没有工具和插件、temperature 为 0 的子 Agent，因此可以沿用现有 Provider，
又不会递归触发 Memory。

## 使用建议

- **自定义后端会在多个运行间共享，也可能被并发访问。** 实现必须保证并发安全，其生命周期
  由创建者管理，插件不会主动关闭。Notes 的“读取—修改—写入”由内部锁串行化，内置 SQLite
  存储也会自行串行化。
- **记什么由模型判断。** 较弱的模型可能漏记该记的，或记下不该记的；准入评测
  （`tests/plugins/test_memory_admission_eval.py`）可以用来检查你打算使用的模型。
  Notes 就是 `MEMORY.md` 里的纯文本，可以随时查看和修改。
- **一次 `recall` 最多会增加两次辅助模型调用。** 默认关键词索引会扩展查询，命中结果还会经过
  归纳；分别设置 `expand_query=False` 和 `summarize_recall=False` 可以关闭。Memory 没有
  其他调用模型的地方。
- **建索引在运行返回前完成。** 配了 `embedder=` 时，每次运行会多一次嵌入请求；失败只记日志，
  不会让运行失败。
- **`recall` 的效果取决于已有归档。** 之前用 `index=None` 的对话，后来就是搜不到；
  Archive 从启用索引的那一刻才开始积累。0.9.311 之前归档的运行仍可检索，保持原先按消息
  拆分的形式。
- **作用域就是它的键。** 以路径为键时，项目目录改名或移动后键就变了，旧记忆留在旧键下。
  一个作用域的历史不会出现在别的作用域的 `recall` 里，也不会出现在不属于作用域的运行里。

## 延伸阅读

- [插件](plugins.md)：Memory 是跨运行状态插件的代表
- [Session 与 Checkpoint](sessions-and-checkpoints.md)：对话内持久化，以及 Transcript 从哪里来
- [Web UI](web-ui.md)：内置 Memory 编辑器
- 示例：[`23_memory.py`](../../examples/23_memory.py)
