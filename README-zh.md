# lovia

[English README](./README.md)

lovia 是一个优雅、克制的 Python Agent 框架，适合希望自己掌控 agent loop，
同时又不想从零拼装所有基础设施的开发者。它提供真实应用迟早会遇到的能力：
工具调用、流式输出、结构化输出、会话、handoff、审批、护栏、workspace、
skills、MCP、上下文压缩、checkpoint/resume 和一个轻量的 Web UI；同时保持核心简洁，方便阅读、替换和扩展。

核心抽象很少：

- `Agent` 是不可变的运行配置；
- `Runner` 负责执行一次 run；
- `@tool` 就是一个带类型注解的 Python 函数；
- `Handoff` 和 `agent.as_tool()` 是组合多 agent 的两个原子抽象；
- plugin 用来打包可复用能力，但不接管控制流。MCP、Skills、Todo、长期记忆等都可以通过 plugin 实现。

这就是 lovia 的取舍：把 agent 应用里真正反复出现的部分做扎实，但不把框架做成一套庞大的平台。

```bash
pip install lovia
```

```python
from lovia import Agent, Skills, Todo, tool
from lovia.workspace import Workspace


@tool
def lookup_ticket(ticket_id: str) -> str:
    """查询内部工单状态。"""
    return f"{ticket_id}: waiting for customer reply"


agent = Agent(
    name="operator",
    instructions=(
        "你是客户支持运营助手。回复客户前先确认工单状态，"
        "再根据团队政策给出清晰、克制、可执行的处理方案。"
    ),
    model="deepseek-v4-pro",
    tools=[lookup_ticket],
    plugins=[Todo(), Skills("./skills")],
    workspace=Workspace.local(".", mode="trusted"),
)

# run_sync() 省去脚本/notebook 里的 asyncio 样板；异步代码里改用
# `await Runner.run(agent, ...)`（见下方 Runner 一节）。
result = agent.run_sync("查看工单 T-1001，并根据团队规范草拟回复。")
print(result.output)
```

这里的 `./skills` 指向 skills 目录；如果暂时没有 skill，可以先删掉
`Skills("./skills")`。

使用 OpenAI 官方接口时设置 `OPENAI_API_KEY`；如果你用 DeepSeek、Ollama、
vLLM 等 OpenAI 兼容接口，设置 `OPENAI_BASE_URL` 即可。Anthropic 也内置支持：
`model="anthropic:claude-4-8-opus"`。

## 为什么是 lovia

lovia 更关心可组合的基础抽象，而不是把所有事情包装成一套新体系。它尽量贴近
Python 本来的样子：dataclass、Protocol、async function、显式组合。

- **代码可读。** `lovia/runner.py` 只是门面；真正的可变运行状态集中在
  `lovia/runtime/loop.py`。当行为不符合预期时，你能沿着很短的路径读进去。
- **模型中立，没有沉重适配层。** 内置 provider 直接用 `httpx` 调 OpenAI
  Chat Completions 和 Anthropic Messages；自定义 provider 只需要实现一个
  `Protocol`。
- **上下文管理可以替换。** 默认 `Compaction` 只改变“下一次发给模型的视图”，
  session 和 checkpoint 仍保存完整 transcript；高级用户也可以实现自己的
  `ContextPolicy`。
- **多 agent 组合很克制。** Handoff 适合把控制权交给另一个专家 agent；
  agent-as-tool 适合把某个 agent 当成可委派的子任务。两者都只是原子抽象，
  不要求你接受一整套编排 DSL。
- **生产能力是明确的接口，不是姿态。** 审批、预算、取消、运行中转向、重试、
  hooks、受权限约束的 workspace 工具、checkpoint/resume 都是你可以接进自己
  产品的显式旋钮。
- **只有一条扩展轴。** Plugin 可以打包工具、系统提示、每轮 view injector、
  hooks、guardrails 和清理逻辑。Skills、MCP、todo list 和长期记忆都可以用
  同一套机制表达。

## 从小脚本长到真实应用

lovia 可以只是一次模型调用的薄包装，也可以随着需求逐步加能力。

| 当你需要…… | 加上…… |
| --- | --- |
| 快速脚本或 notebook helper | `Agent.run_sync(...)` |
| 工具调用 | `@tool` 函数 |
| 类型化最终结果 | `output_type=YourModel` |
| 实时 UI 输出 | `Runner.stream(...)` 和类型化事件 |
| 多轮聊天 | `SQLiteSession` 或你自己的 `Session` |
| 长任务恢复 | `CheckpointOptions` |
| 多 agent 路由或委派 | `handoffs=[...]` 或 `agent.as_tool()` |
| 人类审批 | `@tool(needs_approval=True)` |
| 文件和 shell | `Workspace.local(...)` |
| 长上下文生存 | `Compaction`（自动提供 `recall_tool_result`） |
| 自定义上下文策略 | 实现自己的 `ContextPolicy` |
| 可复用能力包 | `PluginInstance`、`Skills`、`Todo` 或 `MCP` |

## 设计哲学

lovia 的优先级如下：

1. **Concise 简洁。** 一个功能应该能装进脑子里。公共 API 要直观，必要时也能读懂内部实现。
2. **Lightweight 轻量。** 核心应该安装干净、导入迅速，不把你没要的基础设施带进来。
3. **Extensible 易扩展。** 真实应用一定会有自己的 provider、存储、策略、工具和 UI。lovia 提供扩展点，而不是锁死路径。
4. **General-purpose 通用。** 内置能力是实用工具，也是扩展点的示范；你可以用同样的接口替换它们。

它有意保持克制。如果一个功能可以是用户侧十来行代码，就不应该变成框架 API；
如果确实属于框架，也应该接进现有 loop，而不是另起一套控制流。

## 运行时怎么拼起来

每次 run 的形状基本一样：

```text
Agent + input
  -> RunLoop 加载 session/checkpoint 状态
  -> plugins 贡献 tools、instructions、hooks、guardrails、view injectors
  -> context policy 渲染本次发给模型的 view
  -> provider 流式产出类型化 delta
  -> tools、approval、handoff、guardrails、hooks 在明确检查点运行
  -> 把本次 run 自己的 entries 追加进 session；并对该 run 做 checkpoint
```

两个边界尤其重要：

- **Session 和 checkpoint 不同。** `Session` 是多次调用之间的对话记忆；
  checkpoint 是一次幂等长任务的崩溃恢复快照。
- **Transcript 和 view 不同。** Transcript 是事实来源；上下文压缩只渲染更小的
  provider view，让长对话继续跑下去，但不重写历史。

## 核心 API

### Agent

`Agent` 是声明式运行配置，不持有对话状态，因此可以安全地在多个请求之间复用。

```python
from lovia import Agent

agent = Agent(
    name="writer",
    instructions="回答要具体、简洁。",
    model="deepseek-v4-pro",
)
```

动态指令片段会收到本次运行的 `RunContext`（与 tools/hooks 拿到的是同一个句柄，
通过 `ctx.deps` 读取你的依赖对象）：

```python
@agent.instruction
async def user_tier(ctx) -> str:
    return f"用户等级：{ctx.deps['tier']}"
```

需要临时变体时，用 `clone()`，原 agent 不会被修改：

```python
strict = agent.clone(instructions="只输出带引用的回答。")
```

### Runner

```python
from lovia import Runner

result = await Runner.run(agent, "写一段 release note。")
print(result.output)
```

脚本和 REPL 里可以直接从 agent 调用：

```python
result = agent.run_sync("总结这个文件。")
```

`stream()` 返回的 handle 既可以异步迭代，也可以 await：

```python
from lovia import events

handle = Runner.stream(agent, "用一段话解释 context window。")

async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

### Tools

任何带类型注解的 Python callable 都可以变成工具。lovia 会从类型注解、docstring、`Annotated` 和 Pydantic `Field` 元数据生成工具 schema。

```python
from typing import Annotated
from pydantic import Field
from lovia import tool


@tool
async def lookup_order(order_id: str) -> str:
    """按订单号查询订单。"""
    return f"{order_id}: shipped"


@tool(strict=True)
def search_docs(
    query: Annotated[str, "搜索关键词"],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """搜索内部文档。"""
    return []
```

同步工具会在线程池中运行，异步工具会被直接 await。

## 结构化输出

传入 Pydantic model、dataclass、`TypedDict` 或受支持的 Python 类型，最终输出会被自动校验。默认情况下，如果解析失败，lovia 会让模型修正一次。

```python
from pydantic import BaseModel
from lovia import Agent, Runner


class Brief(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(
    name="summarizer",
    model="deepseek-v4-pro",
    output_type=Brief,
)

result = await Runner.run(agent, "给 Python 开发者总结 lovia。")
print(result.output.title)
```

也可以按调用临时覆盖输出类型：

```python
result = await Runner.run(agent, "返回发布清单。", output_type=list[str])
```

## 模型与 Provider

`model` 可以是字符串、provider 实例，也可以是 fallback 链：

```python
from lovia import Agent, ModelSettings

agent = Agent(
    name="assistant",
    model=[
        "anthropic:claude-4-8-opus",
        "deepseek-v4-pro",
    ],
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
```

自定义 provider 实现 `Provider` 协议即可，也可以通过 `lovia.providers` entry point 注册。

## 多 Agent 工作流

### Handoff

handoff 让一个 agent 把控制权交给专家 agent。transcript 会跟随移交，专家 agent 带着完整上下文继续。

```python
from lovia import Agent, Handoff, Runner

billing = Agent(name="billing", instructions="处理账单问题。", model="deepseek-v4-pro")
support = Agent(name="support", instructions="处理技术问题。", model="deepseek-v4-pro")

triage = Agent(
    name="triage",
    instructions="把用户请求路由到合适的专家。",
    model="deepseek-v4-pro",
    handoffs=[billing, support],
)

result = await Runner.run(triage, "我被重复扣款了。")
```

### Agent 作为工具

把 agent 当成可委派的子程序：

```python
summarizer = Agent(
    name="summarizer",
    instructions="用五条要点总结文本。",
    model="deepseek-v4-pro",
)

manager = Agent(
    name="manager",
    instructions="需要总结时交给 summarizer。",
    model="deepseek-v4-pro",
    tools=[summarizer.as_tool(description="总结一段文本。")],
)
```

子 agent 会在独立 loop 中运行，最终输出作为工具结果返回。父 agent 不会持有它的上下文。

## 人类控制

### 工具审批

给敏感操作设置 `needs_approval=True`。

```python
from lovia import tool


@tool(needs_approval=True)
async def refund(order_id: str, amount_cents: int) -> str:
    """执行退款。"""
    return "refunded"
```

流式模式下，你的 UI 可以处理审批事件：

```python
from lovia import events

handle = Runner.stream(agent, "给订单 A123 退款。")

async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()          # 或 ev.reject()
```

服务端也可以设置程序化策略：

```python
agent = Agent(
    ...,
    approval_handler=lambda call, ctx: "ask" if call.name == "refund" else "allow"
)
```

### 主动询问人类

`ask_human` 让模型通过你的应用向操作员请求输入。

```python
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()

agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    tools=[ask_human(channel)],
)

# 在你的 UI 或事件循环中：
for question in channel.pending:
    channel.answer(question.id, "使用方案 A。")
```

## Sessions 与 Checkpoints

Session 用于跨多次调用保存对话 transcript：

```python
from lovia.stores import SQLiteSession

session = SQLiteSession("chat.db")

await Runner.run(agent, "我的项目叫 Atlas。", session=session, session_id="u1")
result = await Runner.run(agent, "我的项目叫什么？", session=session, session_id="u1")
```

Checkpoint 用于长任务的崩溃恢复和幂等运行：

```python
from lovia import CheckpointOptions
from lovia.stores import SQLiteCheckpointer

checkpoint = SQLiteCheckpointer("runs.db")

result = await Runner.run(
    agent,
    "迁移报告格式。",
    checkpoint=CheckpointOptions(checkpoint, "report-migration-42"),
)
```

两个 store 都是 **append-only**：`Session` 累积已结束的 run（每个 run 一个 segment —— 成功完成的，或被调用方定稿的），checkpoint 保存仍可 resume 的那个 run，所以完整 transcript = `session.load()` 加上进行中的 snapshot。历史不可变 —— 每个 run 只追加自己的 entries，从不重写。请给每个 run 一个在单个 checkpointer 内唯一的 `run_id`（如 `uuid4().hex`）——它是 checkpoint 的唯一键，且不像 session 那样按 `session_id` 限定作用域。

## 上下文管理

长对话默认使用 `Compaction`。它只改变“本次发给模型的视图”：完整 transcript 仍保存在 session/checkpoint 中；在上下文有压力时，模型调用前归档超大工具结果、清理较旧工具结果，必要时总结旧历史。

```python
from lovia import Compaction, Runner

policy = Compaction(
    context_window=200_000,
    compact_at=0.75,
    compact_to=0.50,
)

result = await Runner.run(agent, "继续。", context_policy=policy)
```

`Compaction` 会自动提供 `recall_tool_result` 工具，模型可凭 `call_id` 在压缩后无需
重跑工具即可找回某个工具结果——无需手动注册。若要把大体积工具输出归档到存储
（recall 会从中读回，临时存储则回退到 transcript），给策略传入一个 result store：

```python
from lovia.context import Compaction, FileResultStore

policy = Compaction(context_window=200_000, store=FileResultStore(".cache/results"))
```

关闭自动压缩：`from lovia.context import NoopContextPolicy`，并传入
`context_policy=NoopContextPolicy()`。

## 护栏、可靠性与 Hooks

输入和输出护栏都是异步 callable。抛出 `GuardrailTripped` 或返回真值违规消息即可中止运行。

```python
from lovia.exceptions import GuardrailTripped


async def no_email_addresses(messages, ctx):
    if any("@" in str(m.content) for m in messages):
        raise GuardrailTripped("不允许输入邮箱地址。")


async def must_cite(output, ctx):
    if "source:" not in output.lower():
        return "缺少来源引用。"


agent = Agent(
    name="researcher",
    model="deepseek-v4-pro",
    input_guardrails=[no_email_addresses],
    output_guardrails=[must_cite],
)
```

预算、取消和重试策略都是显式传入的：

```python
from lovia import RetryPolicy, RunBudget

result = await Runner.run(
    agent,
    "分析这些日志。",
    budget=RunBudget(max_tool_calls=20, max_seconds=60),
    retry=RetryPolicy(max_attempts=3),
)
```

生命周期 hooks 接收的就是流式输出同一套类型化事件。每个 handler 都以
`handler(event, ctx)` 形式被调用——既拿到事件，也拿到本次运行的 `RunContext`
（运行时动态状态：`session_id`、当前 agent、累计用量……）：

```python
from lovia import RunContext, events
from lovia.hooks import AgentHooks

hooks = AgentHooks()


@hooks.on(events.ToolCallStarted)
async def log_tool(ev, ctx: RunContext):
    print(ev.call.name, ev.call.arguments)


@hooks.on(events.RunCompleted)
async def on_done(ev, ctx: RunContext):
    print("done:", ctx.session_id, ev.result.usage)


agent = agent.clone(hooks=hooks)
```

运行中转向（steering）是取消的入向对偶：往一个正在运行的 run 里 push 一条
消息，模型会在下一个 turn 开始时把它当作普通的 user 消息看到。工具和 hooks
通过 `ctx.mailbox` 拿到同一条通道——没传 `mailbox=` 时 runner 会为每个 run
自动创建——所以 run 也可以在内部转向自己，不需要任何外部接线：

```python
from lovia import Mailbox

mailbox = Mailbox()
handle = Runner.stream(agent, "分析这些日志。", mailbox=mailbox)
mailbox.push("重点看 14:00 左右的 5xx 峰值。")  # 下一个 turn 生效


@hooks.on(events.TurnStarted)
def deadline(ev, ctx: RunContext):
    if ev.turn == 9:
        ctx.mailbox.push("最后一轮：用现有信息直接作答。")
```

## 内置工具

工具不会自动塞进 agent。你按需选择。

```python
from lovia.tools.http import http_fetch
from lovia.tools.search import duckduckgo_search

agent = Agent(
  name="researcher",
  model="deepseek-v4-pro",
  tools=[http_fetch, duckduckgo_search()],
)
```

DuckDuckGo 搜索支持需要安装：

```bash
pip install "lovia[ddg]"
```

如果你有自己的搜索后端，实现 `WebSearch` 并传给 `web_search()` 即可。

## Plugins

**Plugin** 是 lovia 唯一的扩展轴，用来把一个功能打包成一个对象。

单个 plugin 可以贡献任意组合：`tools`、系统提示 `instructions`、每轮注入的 `view_injectors`（临时提醒，永不写入 transcript）、事件 `hooks`，以及 `input_guardrails` / `output_guardrails`。

runner 在**每次 run**（以及 handoff 时每个 agent）通过 await 其异步 `setup()` 来激活每个 plugin，并在 run 结束时通过 `aclose()` 释放它打开的资源。

Plugin 是纯增量的——它们不驱动控制流；中止、重试、handoff 始终由 loop 掌控。下面的 Skills、MCP 和 todo 列表都是内置 plugin。

### Todo 列表

内置 todo plugin 给模型一个清单工具，并在每一轮重新展示当前清单，同时不会让持久化的 transcript 膨胀：

```python
from lovia import Agent, Runner, Todo

agent = Agent(
    name="builder",
    instructions="认真完成多步骤任务。",
    model="deepseek-v4-pro",
    plugins=[Todo()],
)

await Runner.run(agent, "实现一个小型 REST API，包含测试和文档。")
```

### Skills

Skills 是遵循 Agent Skills 规范的可复用指令包。lovia 会先暴露轻量 metadata，让模型判断是否需要；完整指令和引用文件只在需要时加载。

```python
from lovia import Agent, Skills

agent = Agent(
    name="support",
    instructions="根据正确政策帮助客户。",
    model="deepseek-v4-pro",
    plugins=[Skills("./skills")],
)
```

一个 skill 目录包含带 YAML frontmatter 的 `SKILL.md`，也可以包含 `references/`、`scripts/`、`assets/`。可以传入多个目录，或用 filter 控制哪些 skill 暴露给模型：

```python
plugins=[Skills("./skills", "./team-skills")]
plugins=[Skills("./skills", filter=lambda meta: "internal" not in meta.extra.get("tags", []))]
```

如需自定义后端，把 `SkillSource`（或预先构建的 `SkillCategory`）传给 `Skills()` 而不是路径。

### MCP

[Model Context Protocol](https://modelcontextprotocol.io) server 把它们的工具暴露给 agent。安装可选依赖：

```bash
pip install "lovia[mcp]"
```

```python
from lovia import Agent
from lovia.plugins.mcp import MCPServerStdio, MCP

agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    plugins=[
        MCP(MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"]))
    ],
)
```

默认情况下每次 run 会打开并关闭 server。需要跨多次 run 复用同一连接时，打开 session 并传入这个活跃连接：

```python
server = MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])

async with server.session() as conn:
    agent = Agent(name="assistant", model="deepseek-v4-pro", plugins=[MCP(conn)])
    await Runner.run(agent, "抓取 https://example.com 并总结。")
```

`MCP()` 接受多个 server——`MCP(a, b)`——而 `MCPServer.name` 会给某个 server 的工具加前缀（`web__fetch`）以避免冲突。

### 长期记忆

`Memory` 为 agent 提供可跨 run、跨 session 保留的长期记忆。它分为两个层级，并暴露三个模型很容易理解的动作：

- **Notes**（*热*层）——一小段有字符预算的笔记，**每次都会注入**系统提示，用来保存用户的稳定偏好和长期事实。模型可以通过 `remember(fact)` / `forget(fact)` 主动维护它；默认情况下，plugin 也会在 run 结束时自动提取值得长期保存的事实，写入 Notes。
- **Archive**（*冷*层）——支持全文检索的历史对话归档。它不会默认进入上下文，只在模型调用 `recall(query)` 时按需取回相关内容。

```python
from lovia import Agent, Memory

agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    plugins=[Memory("./.lovia/memory")],
)
```

`Memory("./dir")`（或 `Memory()`）会在指定根目录下创建默认实现：一个 Markdown 笔记文件，以及一个 SQLite FTS5 归档库。

```
.lovia/memory/
├── MEMORY.md      # 热层：一行一条长期事实，始终放进上下文
└── archive.db     # 冷层：可检索的历史对话归档
```

> **隐私。** Archive 会把用户和助手的消息文本持久化到磁盘，因此可能保存敏感内容。请把记忆目录放在访问控制合适的位置；如果不希望保留可检索的历史对话记录，请传入 `archive=None`。

可以用可选参数调整行为：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `auto_extract` | `True` | run 结束时用一次模型调用提取长期事实写入 Notes；超出预算时会合并整理 Notes |
| `summarize_recall` | `True` | `recall` 返回由模型整理过的命中摘要，而不是原始片段 |
| `recall_k` | `5` | `recall` 从 Archive 中取回的命中数量 |
| `model` | host 模型 | 用于提取、整理和召回摘要的模型 |

提取、整理和召回摘要这些内部请求，会通过一个没有工具、没有 plugin 的子 agent 调用 `Runner.run`，并使用结构化输出。因此它们能复用同一条 provider 链，又不会递归触发 `Memory` 自身。lovia 的 transcript 会完整保留，context compaction 只影响传给模型的视图，所以事实提取只需要在 run 结束时针对完整 transcript 跑一次：它做的是整理，把少量长期事实放进小而稳定的热层，而不是在上下文丢失后补救。

**自带后端。** 两个层级背后各有一个小协议（`NotesStore`、`ArchiveStore`），所以你可以把任意一层换成自己的实现，比如 Redis、向量库或 Postgres，同时保留同一套工具和 instructions：

```python
from lovia import Agent, Memory

agent = Agent(name="assistant", plugins=[Memory(notes=my_notes, archive=my_archive)])
```

传入 `archive=None` 可以得到只有 Notes、没有 `recall` 工具的记忆。自定义后端是长生命周期对象，会被每次 run 共享，因此需要保证并发安全；plugin 不会替你关闭它们。

### 编写 plugin

一个 plugin 就是任意带有 `name` 和返回 `PluginInstance` 的 `async setup()` 的对象。

需要**每次 run 全新**的状态放在 `setup` 内部（如上面的 todo 列表）；需要**跨 run、跨 session 持久化**的状态则挂在 plugin 上、在构造时传入。

下面是一个术语表 plugin——它包裹一个你自己提供、只创建一次、被每次 run 共享的后端，于是在一次对话里定义的术语，在下一次对话里依然可知。（这正是上面内置 `Memory` plugin 所基于的模式。）

```python
from dataclasses import dataclass
from typing import Protocol

from lovia import Agent, PluginInstance, tool


class Glossary(Protocol):
    """你的共享后端——一个数据库、一个文件、一个内存字典。"""

    async def define(self, term: str, meaning: str) -> None: ...
    async def lookup(self, term: str) -> str | None: ...


@dataclass
class GlossaryPlugin:
    """跨 session 的术语表，agent 可以写入并读回。"""

    store: Glossary  # 长生命周期，被每次 run 共享——不在每次 run 重建
    name: str = "glossary"

    async def setup(self) -> PluginInstance:
        store = self.store

        @tool
        async def define(term: str, meaning: str) -> str:
            """记录某个领域术语的含义，供本次及以后的 session 使用。"""
            await store.define(term, meaning)
            return f"已记录：{term}。"

        return PluginInstance(
            tools=[define],
            instructions="用 `define` 记录用户解释的领域术语。",
        )


store = MyGlossary()  # 你的 Glossary 后端：只需异步的 define() 和 lookup()
agent = Agent(name="assistant", model="deepseek-v4-pro", plugins=[GlossaryPlugin(store)])
```

由于该后端被（可能并发的）多个 run 共享，它必须支持并发访问；而且 plugin 不会关闭它——它的生命周期属于创建它的人。（对比 todo plugin：它的 store 在每次 run 的 `setup` 里重建。）

`PluginInstance` 可携带以下贡献的任意子集：

| 字段 | 作用 |
| --- | --- |
| `tools` | 合并进 agent 的工具集 |
| `instructions` | 追加到系统提示 |
| `view_injectors` | 每轮追加到模型视图的条目——永不持久化 |
| `hooks` | 观察 run 事件的 `AgentHooks`（指标、审计……） |
| `input_guardrails` / `output_guardrails` | 在 loop 的检查点运行，与 agent 自身的一起；中止由 loop 掌控 |
| `aclose` | run 结束时 await，用于释放 `setup` 中打开的资源 |

## Workspace Agents

`Workspace` 会给 agent 增加受 root 目录和权限策略约束的文件/Shell 工具。

```python
from lovia import Agent
from lovia.workspace import CommandRule, Workspace

agent = Agent(
    name="coder",
    instructions="做小而精准的代码修改。",
    model="deepseek-v4-pro",
    workspace=Workspace.local(
        ".",
        mode="coding",
        denied_paths=(".env*",),
        command_rules=(
            CommandRule("pytest", "allow"),
            CommandRule("rm -rf", "deny"),
        ),
    ),
)
```

模式：

| 模式 | 工具 |
| --- | --- |
| `readonly` | `read_file`、`list_files`、`grep_files` |
| `coding` | 读取工具 + `write_file`、`edit_file`、默认需审批的 `shell` |
| `trusted` | coding 工具 + 默认允许的 `shell` |

Workspace 路径都是 root-relative；绝对路径、`..` 逃逸和符号链接逃逸都会被拒绝。本地 shell 仍以宿主机用户身份运行；如果你需要强隔离，请自行实现容器或远程 workspace backend。

## Web UI

可选 Web 层是一个轻量的 FastAPI 应用，包含 SSE 流式输出、sessions、Markdown 渲染和审批路由。

```bash
pip install "lovia[web]"
```

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

### 命令行启动

无需写代码：`python -m lovia.web` 会构建一个默认 agent——模型取自环境变量、技能取自
`./skills`、长期记忆存于 `./.lovia/memory`、一个 todo 清单、内置工具（时间、HTTP
抓取、网页搜索）、并在当前目录开启一个 trusted workspace——然后启动聊天 UI。

```bash
python -m lovia.web                                    # 零配置
python -m lovia.web --port 9000 --model openai:gpt-5.4
python -m lovia.web --skills-dir ./skills --workspace-mode readonly
python -m lovia.web --memory-dir ./mem                 # 记忆存到 ./mem
python -m lovia.web --app myagents:assistant           # 启动你自己的 Agent
```

常用选项也可以用 `LOVIA_*` 环境变量指定（优先级：**命令行 > 环境变量 > 默认值**）；
若装有 `python-dotenv`，会自动加载当前目录下的 `.env`（也可用 `--env-file` 指定）。
模型凭证沿用各 provider 自己的 `OPENAI_API_KEY` / `OPENAI_BASE_URL`（Anthropic 用 `ANTHROPIC_*`）。

| 选项 | 环境变量 | 默认值 |
| --- | --- | --- |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--db` | `LOVIA_DB` | cwd 下的 `<agent>.db` |
| `--model` | `LOVIA_MODEL` → `OPENAI_DEFAULT_MODEL` → `ANTHROPIC_DEFAULT_MODEL` | 必填 |
| `--skills-dir`（可重复） | `LOVIA_SKILLS_DIR` | 存在则用 `./skills` |
| `--memory-dir` / `--no-memory` | `LOVIA_MEMORY_DIR` | `./.lovia/memory`（默认开启） |
| `--workspace` / `--workspace-mode` | `LOVIA_WORKSPACE` / `LOVIA_WORKSPACE_MODE` | `.` / `trusted` |
| `--instructions-file` | `LOVIA_INSTRUCTIONS_FILE` | `AGENTS.md`，否则用通用提示 |
| `--app MODULE:ATTR` | `LOVIA_APP` | 构建默认 agent |
| `--max-retries` | `LOVIA_MAX_RETRIES` | `2`（首次之后的重试次数；`0` 关闭） |
| `--provider-timeout` | `LOVIA_PROVIDER_TIMEOUT` | `60` 秒 |
| `--max-tokens` | `LOVIA_MAX_TOKENS` | provider 默认值 |
| `--context-window` | `LOVIA_CONTEXT_WINDOW` | 自动检测，否则 200K |
| `--max-turns` | `LOVIA_MAX_TURNS` | `50` |
| `--trust-env` | `LOVIA_PROVIDER_TRUST_ENV` | 关闭（开启后读取 `HTTP(S)_PROXY`） |

`--provider-timeout` 和 `--trust-env` 由各 provider 直接读取，因此对 `--app` agent
和库调用同样生效；`--max-retries` / `--max-turns` 作用于每一次服务运行，而
`--max-tokens` / `--context-window` 仅配置默认 agent。

内网自签名 CA 场景：`LOVIA_HTTP_CA_BUNDLE` 让所有出站 HTTPS（模型 provider 和
`http_fetch` 工具）都使用指定的 PEM 证书包，`LOVIA_HTTP_INSECURE=1` 则关闭校验
（仅限可信网络）。`web` extra 已内置 `truststore`，会自动信任操作系统证书库
（与浏览器一致，无需任何配置）。

默认 agent 还会内置一组常用能力：`todo_write` 清单，以及 `now`（时间）、`http_fetch`、
`web_search` 工具。网页搜索需要 `ddg` extra（已包含在 `lovia[web]` 中）；若未安装则
仅跳过该工具。

`--version` 打印版本号；完整选项见 `python -m lovia.web --help`。

### 用 API 自建 UI

HTTP API 与内置聊天页面是解耦的，你可以保留 JSON + SSE 接口、换上自己的前端。
既可以关掉内置 UI：

```python
from lovia.web import create_app

app = create_app(agent, ui=False)   # 不挂载 GET / 与 /static，只暴露 API
```

也可以把这套无 UI 的 router 挂进你自己的 FastAPI 应用：

```python
from fastapi import FastAPI
from lovia.web import RouterDeps, build_api_router, ChatStore
from lovia.web.approvals import ApprovalRegistry

deps = RouterDeps(
    agents={"bot": agent},
    store=ChatStore.in_memory(),
    approvals=ApprovalRegistry(),
)
app = FastAPI()
app.include_router(build_api_router(deps))
```

主要接口（完整 schema 见 `/api/docs`）：

| 方法与路径 | 用途                                           |
| --- |----------------------------------------------|
| `GET /api/info` | agents、版本、能力开关                               |
| `GET /api/agents`、`GET /api/agents/{name}` | 列出 / 获取 agent                                |
| `POST /api/chat` | 同步聊天，返回完整结果 → `{output, session_id, usage}`      |
| `POST /api/chat/stream` | 流式聊天，通过 SSE 返回增量事件（`text_delta`、`tool_call`、`done` 等） |
| `POST /api/chat/approve`、`POST /api/chat/cancel` | 审批 / 取消请求                                    |
| `GET /api/sessions` | 会话列表（`?q=` 搜索、`?limit=`）；`DELETE` 清空全部       |
| `GET`/`PATCH`/`DELETE /api/sessions/{id}` | 查看 / 重命名 / 删除                                |
| `GET /api/sessions/{id}/export?format=md\|json\|txt` | 导出会话                                         |
| `GET`/`POST /api/schedules`、`DELETE`/`PATCH /api/schedules/{id}` | 定时任务列表 / 创建 / 删除 / 暂停（cron · 间隔 · 定时） |

`lovia/web/static/js/api.js` 是一个开箱即用的浏览器客户端（含 SSE 读取器）——
直接 import，或作为任意语言的参考实现。

## 示例

`examples/` 目录是一组可直接运行的脚本，可以按下面的顺序浏览：

| 路径 | 展示内容 |
| --- | --- |
| `examples/01_hello.py` | 最小 agent |
| `examples/02_tools.py` | 工具调用 |
| `examples/03_streaming.py` | 流式事件 |
| `examples/04_structured_output.py` | 校验后的结构化输出 |
| `examples/05_handoff.py` | 专家 agent handoff |
| `examples/06_agent_as_tool.py` | 把 agent 当作工具委派 |
| `examples/07_session.py` | 持久化聊天历史 |
| `examples/08_skills.py` | 可复用 skill 指令包 |
| `examples/10_hooks.py` | 生命周期事件 hooks |
| `examples/11_approval.py` | 人类审批 |
| `examples/14_guardrails.py` | 输入/输出护栏 |
| `examples/15_resume.py` | checkpoint 与 resume |
| `examples/16_web_serve.py` | 内置 Web UI |
| `examples/17_web_api.py` | 仅 API 的服务 + 自建前端 |
| `examples/18_context_policy.py` | 只改变视图的上下文压缩 |
| `examples/20_custom_provider.py` | 实现 `Provider` 协议（离线可跑） |
| `examples/21_dx.py` | 同步调用、临时输出类型等 DX 快捷方式 |
| `examples/23_workspace_agent.py` | 受权限约束的代码 workspace |
| `examples/24_steering.py` | 运行中注入消息（调用方与 hook 双侧 steering） |
| `examples/25_data_analysis.py` | 数据分析 agent |
| `examples/26_mcp.py` | MCP server 工具 |
| `examples/27_todos.py` | todo plugin 和每轮提醒 |
| `examples/28_memory.py` | 用 `Memory` plugin 跨 run 的长期记忆 |
| `examples/workflows/` | prompt chaining、routing、parallelization、evaluator loop、自主 agent |

## 安装 Extras

| 需求 | 安装 |
| --- | --- |
| 核心框架 | `pip install lovia` |
| DuckDuckGo 搜索 | `pip install "lovia[ddg]"` |
| MCP 集成 | `pip install "lovia[mcp]"` |
| Web UI | `pip install "lovia[web]"` |
| 运行示例 | `pip install "lovia[examples,web]"` |
| 开发、测试、发布 | `pip install -e ".[dev]"` |

`examples` 是运行演示脚本所需的依赖，例如 `python-dotenv`、`rich` 和 `ddgs`。`dev` 是维护这个仓库所需的依赖，例如 `pytest`、`ruff`、`mypy`、`build`、`twine` 以及 Web 测试依赖。二者故意分开，避免普通开发安装演示专用依赖。

## 开发

```bash
pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format .
.venv/bin/python -m mypy lovia
```

`examples/` 目录里有主要能力的可运行脚本。真实 provider 端到端测试带有 `live_provider` 标记，默认不会运行。
