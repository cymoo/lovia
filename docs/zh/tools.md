# 工具

工具是模型可以调用的带类型 Python 函数。lovia 会从签名推导 JSON Schema，在你的代码
运行前校验参数，并处理循环机制：并发、重试、超时、截断。这样工具本身仍然只是一个
普通函数。

```python
from typing import Annotated

from pydantic import Field

from lovia import tool


@tool
async def lookup_order(order_id: str) -> str:
    """根据订单号查询订单。"""
    return f"{order_id}: 已发货"


@tool(strict=True)
def search_docs(
    query: Annotated[str, "搜索关键词"],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """搜索内部文档。"""
    return []
```

通过 `Agent(tools=[...])` 挂载工具。同步函数会在线程池里运行，不会阻塞事件循环；
`async def` 函数会直接 await。

## Schema 推导

模型会看到 `name`、`description`，以及参数的 JSON Schema：

- **name**：默认是函数名，除非 `@tool(name=...)` 覆盖。
- **description**：来自 docstring（或 `@tool(description=...)`）。这是模型判断
  **什么时候**调用工具的唯一指导，请写给模型看，而不是写给同事看。
- **parameters**：来自类型标注。默认值让参数变成可选；`Annotated[T, "text"]`
  添加纯字符串描述；`Annotated[T, Field(...)]` 携带完整 Pydantic 约束
  （边界、pattern、描述）。Pydantic model、dataclass、`TypedDict`、literal、
  union 都可以作为参数类型。
- **`strict=True`**：把 schema 标记为 `additionalProperties: false`，并让每个参数
  都必填，对齐 OpenAI strict mode。

参数会在函数运行前按签名校验（并转换类型）。无效参数不会进入你的代码，而是变成
`InvalidToolArguments` 错误结果，带一个紧凑的校验消息，让模型修正调用。确定性的
参数错误不会重试。

## 接收运行上下文

把一个参数标注为 `RunContext`（名字随意），runner 就会注入本次运行的实时句柄：
依赖、transcript、usage、mailbox、cancel token。

```python
from lovia import RunContext, tool


@tool
async def save_note(ctx: RunContext, text: str) -> str:
    """为这段对话保存一条笔记。"""
    await db.save(ctx.session_id, text)
    return "已保存"
```

这个 context 参数不会出现在模型看到的 schema 中。最多只能有一个参数带这个标注，
否则会抛 `UserError`。完整字段目录见[核心概念](concepts.md#runcontext唯一的运行句柄)。

## 错误语义

工具抛出异常**不会**结束运行。runner 会捕获它，把 `"Tool error: ..."` 字符串作为
本次调用结果返回给模型，让模型自己调整：换参数重试、选择另一个工具，或解释问题。
如果你想主动控制这条错误消息，可以抛 `ToolError`（可带 `hint=`）。

三个异常是特殊的：

- `InvalidToolArguments`：确定性错误；不重试，直接变成错误结果。
- `RunCancelled`：运行级信号；会重新抛出并结束运行。
- `BudgetExceeded`：有作用域区别。由本次运行自己的预算抛出时，会在下一个安全点结束
  运行；如果发生在被委派的 [agent-as-tool](multi-agent.md#agent-as-tool) 子运行里，
  它是可恢复的委派失败，会变成工具错误结果。

## 并发执行与屏障

模型在同一轮请求多个调用时，工具**默认并发执行**。顺序敏感的工具可以退出并发：

```python
@tool(parallel=False)
async def apply_migration(name: str) -> str:
    """执行数据库迁移；不能和其他工具并发。"""
    return "applied"
```

`parallel=False` 会让这个调用成为**执行屏障**：本轮已经在跑的调用先完成，
这个工具单独运行，然后剩下的调用继续。一轮里如果全是屏障工具，就会退化成完全串行执行。

实践中重要的细节：

- [Handoff](multi-agent.md) 工具永远是屏障，不管 `parallel` 怎么设。这保证“同一轮里
  第一个 handoff 胜出”没有竞态。内置工作区 mutator（`write_file`、`edit_file`、
  `shell`）默认 `parallel=False`；只读工具保持并发。
- Preflight（预算检查、审批、参数校验）总是按请求顺序串行执行，所以审批提示
  和预算计数在并发执行时仍然确定。
- 结果按完成顺序 checkpoint 并追加进 transcript；下游都通过 `call_id` 配对调用和
  结果，所以顺序只是展示问题。
- 不同调用的流式事件会交错；用 `ev.call.id` 关联
  （见[流式输出](streaming.md#工具与审批)）。
- `parallel=` 控制的是**执行**。请求侧的对应项，也就是模型是否可以在同一轮发出
  多个工具调用，是 `ModelSettings.parallel_tool_calls`
  （见 [Provider](providers.md#modelsettings)）。

## 重试与超时

```python
@tool(retries=2, timeout=10.0)
async def flaky_lookup(key: str) -> str:
    """从一个偶尔抖动的服务读取数据。"""
    ...
```

- `retries`：首次失败后的重试次数（默认 `0`）；两次尝试之间是指数退避，最大 5s。
  `None` 表示继承 agent 的 `default_tool_retries`。
- `timeout`：每次尝试的秒数；`None` 表示继承 agent 的 `default_tool_timeout`
  （默认无超时）。
- 取消、预算耗尽和参数无效永不重试，因为它们不会自己变好。

## 输出截断

工具输出在进入 transcript 前会被限制长度：优先使用工具级
`@tool(max_output_chars=...)`，否则使用 agent 的 `max_tool_output_chars`
（默认 **200,000 字符**，是防止失控载荷撑爆上下文的保险线，不是策略）。更长的输出会保留
头部和尾部，并加上说明剪掉了多少内容的标记；原始返回值会被丢弃。

这是有意的有损处理：它从源头限制内存、checkpoint 和 session 成本。
`recall_tool_result` 看到的也是截断版本。某个工具如果必须保留完整输出，应该把内容写到
[工作区](workspace.md)，然后返回路径。（这和[上下文压缩](context.md)不同；压缩是
无损、只作用于 view 的。）

## 结果渲染器

模型收到的是字符串。默认规则：字符串原样通过，其他值 JSON 序列化（Pydantic model、
dataclass、enum、日期、path 都会处理）。可以按工具或按 agent 覆盖：

```python
@tool(result_renderer=lambda rows, ctx: format_as_markdown_table(rows))
async def top_customers(n: int = 10) -> list[dict]: ...
```

解析顺序：工具自己的 `result_renderer`，否则 agent 的 `tool_result_renderer`，否则默认
渲染器。渲染器只处理**成功**结果；runner 生成的 `"Tool error: ..."` 字符串会绕过
它们。原始、未渲染的值仍然会通过 `ToolCallCompleted.result` 到达观察者。

## 工具策略

如果要围绕**单次尝试**组合横切行为，如缓存、脱敏、限流、自定义鉴权，可以组合
`ToolPolicy` callable，而不是手写包装函数：

```python
async def cache_policy(invoke, args, ctx):
    key = ("search_docs", tuple(sorted(args.items())))
    if key in cache:
        return cache[key]
    result = await invoke(args, ctx)
    cache[key] = result
    return result


@tool(policies=[cache_policy])
async def search_docs(query: str) -> list[str]: ...
```

policy 接收 `(invoke, args, ctx)`：链上的下一个 callable、**原始**（尚未校验的）
参数、本次运行上下文。它可以修改参数、短路、内部循环或转换结果。多个 policy 按列表
顺序组合（第一个在最外层）；框架重试和 timeout 包住**整条**链，所以每个 policy 每次
只看到一次尝试。参数校验发生在最内层、函数边界；需要转换后值的 policy 要自己校验。

如果门禁需要的是**人的决策**，而不是代码逻辑，请使用 `needs_approval`，见[人工介入](human-in-the-loop.md)。

## 程序化构建工具

`@tool` 是 `Tool` dataclass 的便利封装。`Tool` 包含 `name`、`description`、
`parameters`、`invoke`，以及上面提到的 policy 字段。闭包里带配置的工厂可以直接返回
`Tool` 值；内置的 `web_search(impl)` 和 `ask_human(channel)` 就是例子。如果工具还要
带提示词或生命周期，请用[插件](plugins.md)打包。

## 容易踩的点

- **每个 agent 的工具名必须唯一**，不管来源是 agent 工具、插件、工作区还是 handoff。
  冲突会在运行开始时抛 `UserError`。MCP 服务器给工具加前缀正是为了避免这个问题。
- **被取消的同步工具仍会跑完。** 取消不能中断 worker thread；运行结束后，调用的副作用
  仍可能发生。长耗时或有副作用的工具尽量写成 `async def`。
- **policy 看到的是原始参数。** 默认值还没应用，类型也还没转换；在这一层请把 `args`
  当成不可信模型输出。
- **截断上限按渲染结果的字符数计算。** 200k 字符约等于 50k tokens。如果你的工具确实
  会合法返回更多内容，请提高上限或把载荷持久化到别处；直接丢掉中间内容更糟。

## 延伸阅读

- [内置工具](built-in-tools.md)：HTTP、搜索、时间
- [人工介入](human-in-the-loop.md)：审批门禁
- [插件](plugins.md)：把工具和 instructions、生命周期一起打包
- 示例：[`02_tools.py`](../../examples/02_tools.py)
