# 上下文管理

长对话终会超出上下文窗口。许多框架会直接改写历史，导致事后无法确认模型当时究竟
看到了什么。lovia 的上下文策略**只调整视图**：运行记录（transcript）和 Session 始终保留
完整内容，只有每次发送给 Provider 的视图会被压缩。这样，“模型遗忘”和“记录丢失”仍是
两个彼此独立的问题。

```python
from lovia import Agent, Compaction

agent = Agent(
    name="companion",
    model="glm-5.2",
    context_policy=Compaction(
        context_window=200_000,
        compact_at=0.85,
        compact_to=0.60,
    ),
)
```

上下文策略属于 Agent 的**处理策略**：配置一次，后续每次运行都会继承；也可以用
`Runner.run(..., context_policy=...)` 覆盖某次调用。默认就是 `Compaction()`；只有默认不合适时
才需要配置。用 `NoopContextPolicy` 可以关闭：

```python
from lovia.context import NoopContextPolicy

agent = Agent(..., context_policy=NoopContextPolicy())
```

## 压缩机制

每轮模型调用前，策略会按窗口估算 transcript 大小。接近上限时，它会按**低成本优先**的三阶段渲染
出一个更小的 view：

1. **转存巨大工具结果**（`OffloadToolResults`，≥4,000 字符）：在 view 中替换为 400 字符
   预览标记；如果配置了[结果存储](#结果存储)，完整输出会归档。
2. **清理较旧工具结果**（`ClearToolResults`）：替换成短标记，同时保留最新几个原文。
3. **总结旧历史**（`SummarizeHistory`）：用增量 LLM summary 替换最早的一段历史。summary
   使用结构化章节（session intent、current state、key facts、artifacts、constraints、
   next steps），不会写成随意散文。

这些标记会保留配对关系（`call_id`、错误标记），并告诉模型如何取回内容：

```text
[Earlier tool result cleared to save context.
 Call recall_tool_result("call_42") to retrieve the full output.]
```

`recall_tool_result` 由策略**自动提供**，不需要手动接入。它先读结果存储，再回退到 transcript，
所以恢复内容永远不会重新执行有副作用的工具。

这个设计建立在三个保证上：

- **决策可延续，前缀稳定。** 各阶段会记录决策（已清理 id、转存记录、正在维护的 summary）；
  每轮 view 都从这些决策重新渲染。决策是单调的，所以渲染出的 prompt 前缀在各轮之间保持字节稳定，
  这正是 [provider prompt cache](providers.md#提示词缓存) 能持续命中的原因。压缩和缓存可以配合使用，
  并不冲突。
- **受保护的尾部。** 最近一段内容不会压缩（默认：可用窗口的 20%，至少包含最新用户消息，并且
  始终保持完整 call/result 对）。模型始终能原样看到紧邻上下文。
- **溢出后备。** 如果 provider 仍然拒绝 prompt（`ContextOverflowError`），策略有一次机会
  渲染更激进的 view（尾部收紧到 10%，阈值降低，目标约为可用窗口 25%）并重试本轮。只有重建的
  view 明显更小时才重试，否则错误会向外暴露。

每次压缩都会发出 [`ContextCompacted` 事件](streaming.md#转移与上下文)，其中带有
`CompactionNotice`（原因、压缩前后 token、便于阅读的 detail）。Web UI 会实时渲染它，并在重新加载时
回放最后一个 notice。

## 配置

```python
Compaction(
    context_window=None,        # token；None = 向 provider 询问
    compact_at=0.85,            # 触发水位
    compact_to=0.60,            # 压缩后的目标
    keep_recent_tokens=None,    # 受保护尾部；None = usable // 5
    reserve_output_tokens=16_384,
    stages=None,                # 你自己的 pipeline；None = 上面三阶段
    summarizer=None,            # 你自己的 Summarizer；None = LLMSummarizer()
    image_tokens=1_600,         # 每个 image part 的固定估算
    store=None,                 # 转存输出用的 ResultStore
)
```

- **水位**可以是可用窗口比例（`0.85`），也可以是绝对 token 数（`150_000`）。“可用” =
  window − `reserve_output_tokens`。低于 `compact_at` 时不处理；越界后会把 view 缩到
  `compact_to`（有滞后，避免策略在边界抖动）。
- **`context_window=None`** 会从端点解析窗口——它的 `/models` 列表，或者它第一次拒绝 prompt 时
  点名的上限——最后才回落到适配器的表。完整链路见[上下文窗口](providers.md#上下文窗口)。
  端点点名的上限总会压住你配置的值，所以表里没有的模型代价是撞墙一次，之后整个 session 都按
  真实值计算。只有当**谁都报不出来**时，才会跳过主动压缩、只留 reactive overflow 兜底。
- **token 计数**是校准过的估算：UTF-8 字节数/4 启发式——中文按 ~0.75 token/字计入，
  而不是被纯字符计数低估 4 倍——图片/文件有固定成本，加上请求携带的工具 schema
  （这块固定的加性负载若混进乘性系数会把它带偏），再用 provider 返回的**实际**
  input token 数做 EMA 修正。provider 可以实现 `TokenEstimator` 提供精确计数。

## 结果存储

如果转存输出需要在 view 之外长期存在，就要给它一个存放位置：

```python
from lovia.context import Compaction, FileResultStore

policy = Compaction(context_window=200_000, store=FileResultStore(".cache/results"))
```

`ResultStore` 只有两个方法：`put(key, content)` / `get(key)`，以输出的**内容摘要**为 key——
store 跨 session 共享而 call_id 是会话局部的，摘要键让跨会话撞键从构造上不可能（相同输出
还能免费去重）；offload 标记交给模型的 recall 引用就是这个摘要。
`FileResultStore(dir)` 每个结果写一个文件（不做驱逐，保留策略由你负责）；
`InMemoryResultStore(max_entries=1024)` 是有界 LRU。没有 store 时，转存标记仍然可用，
recall 会回退到 transcript；但如果之后做
[session `trim_tool_results`](sessions-and-checkpoints.md#维护)，没有归档过的内容会被永久截断。

## 压缩状态如何保存

可延续的决策（已清理 id、转存预览、summary + 覆盖范围、校准比例）会序列化进运行的
checkpoint，并在运行结束时写入 session segment 的 `meta`。所以下一次同一 session 的运行会沿用
之前的决策，而不是重新推导；[恢复运行](sessions-and-checkpoints.md)也会从压缩过的位置精确继续。
已总结前缀的结构指纹可以检测被离线改写的历史（比如 trim），并重置 summary，同时保留 id-keyed
决策。

## 自定义上下文策略

扩展有两层深度。**自定义阶段**保留 Compaction 的机制（水位、尾部、状态、marker），只替换“压缩什么”：

```python
class DropOldImages:                      # implements Stage
    name = "drop_images"
    async def plan(self, body, ctx) -> bool:
        ...   # 把决策记录到 ctx.state；如果有新决策则返回 True
```

```python
policy = Compaction(stages=[DropOldImages(), ClearToolResults()])
```

stage 只做 *plan*（记录可延续的决策）；渲染是 transcript + state 的纯函数。stage 不要撤销已有决策：
单调性是保持 prefix cache 稳定的关键。stage 的 `ctx` 是 `StageContext`
（request、sticky `CompactionState`、`TokenCounter`、`TokenBudget`、受保护尾部边界、aggressive flag）。
Compaction 自己用到的部件也导出了，方便复用：`render_view`、`clear_marker` /
`offload_marker` / `summary_entry` builder、`transcript_to_text`、`OffloadRecord` /
`SummaryState`，以及 summarizer 的 `REQUIRED_SECTIONS` / `SUMMARY_SYSTEM_PROMPT` /
`SUMMARY_WRAPPER` 模板。要定制 summary，请配置 `LLMSummarizer(prompt=...,
required_sections=...)`，不要 fork 这段实现。

**自定义 `ContextPolicy`** 则替换全部机制：一个方法
`async compact(req: CompactionRequest) -> ContextResult`。request 携带只读 entries、provider、
`last_input_tokens`、`overflow` flag、`reported_window`（端点拒绝上一个 prompt 时点名的上限——
请记住它，它的优先级压过所有其他窗口来源），以及 runner 会帮你在 checkpoint 中往返保存的
`scratch` dict。
返回 view，加上 `changed`/`compacted` 标志和可选 token 数。可选 `tools()` 方法可以贡献工具；
`lovia.tools.recall` 里的 `make_recall_tool(store)` 是 `Compaction` 用来提供 recall 的工厂，
任何会丢内容的策略都可以复用。`lovia/context/policy.py` 很短，一屏就能读完。

## 注意事项

- **Compaction 不是内存上限。** **transcript** 保留完整输出；只有 view 缩小。失控载荷要在源头由
  [工具输出截断](tools.md#输出截断)限制；那是有损的，且 `recall_tool_result` 也只能看到截断版本。
- **summary 会花一次模型调用**，用的是本次运行自己的 provider（temperature 0）。连续 summary 失败会
  触发每次运行的 circuit breaker（aggressive 路径作为 half-open 探测保留），节省不到 ≥10% 时也会跳过。
  预算敏感的部署需要注意：第 N 轮里可能包含一次额外的 LLM 调用。
- **未知模型的第一次撞墙是一次真实的失败请求。** 正是端点的这次拒绝教会了 lovia 窗口大小；
  紧随其后的压缩会以可用窗口的 ~25% 为目标，而不是主动压缩的 60%——它下手重一倍多。
  知道窗口就请提前设置 `context_window=...`。Ollama 压根不会撞墙
  （它会[静默截断](providers.md#注意事项)），因此必须显式配置。
- **不要在窗口不同的 agent 间共享同一个 `Compaction` 实例。** 状态是按运行/session 的，但配置窗口属于
  policy 实例。clone agent 会共享 policy 实例；变体请各自配置一个。

## 延伸阅读

- [核心概念：运行记录与模型视图](concepts.md#运行记录与模型视图)
- [Provider](providers.md#上下文窗口)：窗口报告与缓存
- [Session 与 Checkpoint](sessions-and-checkpoints.md)：携带状态
- 示例：[`17_context_compaction.py`](../../examples/17_context_compaction.py)
