# lovia

[English README](./README.md)

lovia 是一个优雅而克制的 Python Agent 框架，适合那些希望自己掌控 agent
运行循环、又不想从零搭起所有配套能力的开发者。它提供真实 agent 应用迟早会用到的
组件：工具调用、流式输出、结构化输出、会话、移交、审批、护栏、工作区、技能、
MCP、上下文压缩、检查点与恢复，以及一个轻量 Web UI；同时保持核心足够直接，
方便阅读、替换和扩展。

核心抽象很少：

- `Agent` 是不可变的运行配置；
- `Runner` 负责执行一次运行；
- `@tool` 就是一个带类型注解的 Python 函数；
- `Handoff` 和 `agent.as_tool()` 是组合多个 agent 的两个基本方式；
- 插件用于打包可复用能力，但不接管控制流。MCP、Skills、Todo 和长期记忆都可以通过插件实现。

这就是 lovia 的取舍：把 agent 应用里反复出现的难点处理好，但不把框架做成一整套庞大的平台。

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

# run_sync() 省掉脚本和 notebook 里的 asyncio 样板代码；异步代码里改用
# `await Runner.run(agent, ...)`（见下方 Runner 一节）。
result = agent.run_sync("查看工单 T-1001，并根据团队规范草拟回复。")
print(result.output)
```

这里的 `./skills` 指向团队技能目录；如果暂时没有技能，可以先删掉
`Skills("./skills")`。

使用 OpenAI 官方接口时，设置 `OPENAI_API_KEY`；如果使用 DeepSeek、Ollama、
vLLM 等 OpenAI 兼容服务，则设置 `OPENAI_BASE_URL`。Anthropic 也已内置支持：
`model="anthropic:claude-4-8-opus"`。

## 为什么选择 lovia

lovia 偏爱可组合的基础部件，而不是为所有事情另造一套体系。它尽量贴近普通
Python：dataclass、Protocol、异步函数，以及显式组合。

- **代码好读。** `lovia/runner.py` 只是门面；真正的可变运行状态集中在
  `lovia/runtime/loop.py`。遇到意外行为时，顺着很短的路径就能读到核心逻辑。
- **模型中立，适配成本低。** 内置 provider 直接通过 `httpx` 调用 OpenAI
  Chat Completions 和 Anthropic Messages；自定义 provider 只需要实现一个
  `Protocol`。
- **上下文管理可以替换。** 默认的 `Compaction` 只改变“下一次发给模型的视图”，
  session 和 checkpoint 仍保留完整 transcript；高级用户也可以实现自己的
  `ContextPolicy`。
- **多 agent 组合保持克制。** Handoff 用来把控制权交给专家 agent；
  agent-as-tool 用来把一个 agent 当作有边界的子任务委派出去。二者都是基础部件，
  不是一套必须全盘接受的编排 DSL。
- **面向生产，但不做表面文章。** 审批、预算、取消、运行中指引、重试、hooks、
  受权限约束的工作区工具、检查点与恢复，都是可以接入自己产品的明确接口。
- **扩展只有一条主线。** 插件可以打包工具、系统提示、每轮视图注入器、hooks、
  guardrails 和清理逻辑。Skills、MCP、todo list 和长期记忆都沿用同一套机制。

## 从小开始，按需扩展

lovia 可以只是一次模型调用的薄封装；等产品真的需要，再逐步加能力。

| 当你需要…… | 加上…… |
| --- | --- |
| 快速脚本或 notebook 辅助 | `Agent.run_sync(...)` |
| 工具调用 | `@tool` 函数（默认并发执行） |
| 副作用不可交叠的工具 | `@tool(parallel=False)` |
| 类型化最终结果 | `output_type=YourModel` |
| 实时 UI 输出 | `Runner.stream(...)` 和类型化事件 |
| 多轮聊天 | `SQLiteSession` 或你自己的 `Session` |
| 长任务恢复 | `CheckpointOptions` |
| 多 agent 路由或委派 | `handoffs=[...]` 或 `agent.as_tool()` |
| 人类审批 | `@tool(needs_approval=True)` |
| 文件和 shell 命令 | `Workspace.local(...)` |
| 长上下文续航 | `Compaction`（自动提供 `recall_tool_result`） |
| 自定义上下文策略 | 实现自己的 `ContextPolicy` |
| 可复用能力包 | `PluginInstance`、`Skills`、`Todo` 或 `MCP` |

## 设计哲学

lovia 优先优化四件事，顺序很重要：

1. **Concise 简洁。** 一个功能应该能装进脑子里。公共 API 要直观；需要调试时，内部实现也应该读得懂。
2. **Lightweight 轻量。** 核心要安装干净、导入迅速，不把你没要求的基础设施一起带进来。
3. **Extensible 易扩展。** 真实应用一定会有自己的 provider、存储、策略、工具和 UI。lovia 提供扩展点，而不是锁死路径。
4. **General-purpose 通用。** 内置能力应该实用，但不神秘；它们只是你也能使用的扩展点示例。

lovia 在设计上始终追求“克制”。如果一个功能用用户侧十来行代码就能完成，就不该变成框架 API；
如果确实该进框架，也应该接入现有循环，而不是另起一套控制流。

## 各部分如何协同

每次运行的流程大致相同：

```text
Agent + input
  -> RunLoop 加载 session/checkpoint 状态
  -> plugins 提供 tools、instructions、hooks、guardrails、view injectors
  -> context policy 渲染本次发给模型的 view
  -> provider 以流式方式产出类型化 delta
  -> tools、approval、handoff、guardrails、hooks 在明确的检查点运行
  -> 将本次 run 的 entries 追加进 session；同时为该 run 写入 checkpoint
```

两个边界尤其重要：

- **Session 和 checkpoint 不同。** `Session` 是多次调用之间的对话记忆；
  checkpoint 是一次幂等长任务的崩溃恢复快照。
- **Transcript 和 view 不同。** Transcript 是事实来源；上下文压缩只渲染更小的
  provider view，让长对话继续推进，但不会重写历史。

## 核心 API

### Agent

`Agent` 是声明式的运行配置，不保存对话状态，因此可以安全地在多个请求之间复用。

```python
from lovia import Agent

agent = Agent(
    name="writer",
    instructions="回答要具体、简洁。",
    model="deepseek-v4-pro",
)
```

动态指令片段可以读取本次运行的上下文：

```python
@agent.instruction
async def user_tier(ctx) -> str:
    return f"用户等级：{ctx.deps['tier']}"
```

需要为某次请求临时调整配置时，用 `clone()`；原 agent 不会被修改：

```python
strict = agent.clone(instructions="只输出带引用的回答。")
```

### Runner

```python
from lovia import Runner

result = await Runner.run(agent, "写一段 release note。")
print(result.output)
```

在脚本和 REPL 里，也可以直接从 agent 调用：

```python
result = agent.run_sync("总结这个文件。")
```

`stream()` 返回的 handle 既可以异步迭代，也可以等待最终结果：

```python
from lovia import events

handle = Runner.stream(agent, "用一段话解释 context window。")

async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

### Tools

任何带类型注解的 Python callable 都可以变成工具。lovia 会根据类型注解、
docstring、`Annotated` 和 Pydantic `Field` 元数据生成工具 schema。

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

同步工具会在线程池中运行；异步工具会被直接 await。

模型在同一轮里请求多个工具调用时，这些调用**默认并发执行**。副作用不允许
交叠的工具可以用 `parallel=False` 退出并发：该调用会成为一道执行屏障——先
等本轮所有在飞调用完成，再独占执行，之后其余调用继续。

```python
@tool(parallel=False)
async def apply_migration(name: str) -> str:
    """应用数据库迁移（绝不与其他工具并发）。"""
    return "applied"
```

Handoff 工具与内置的 workspace 变更类工具（`write_file`、`edit_file`、
`shell`）默认即为屏障；只读工具保持并发。同一轮的工具事件在事件流中可能
交错，请通过 `event.call.id` 关联。

## 结构化输出

传入 Pydantic model、dataclass、`TypedDict` 或受支持的 Python 类型后，
最终输出会自动经过校验。默认情况下，如果解析失败，lovia 会让模型修正一次。

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

也可以在单次调用时临时覆盖输出类型：

```python
result = await Runner.run(agent, "返回发布清单。", output_type=list[str])
```

## 模型与 Provider

`model` 可以是模型字符串、provider 实例，也可以是一条 fallback 链：

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

自定义 provider 只需实现 `Provider` 协议；也可以通过 `lovia.providers` entry point 注册。

## 多 Agent 工作流

### Handoff

Handoff 允许一个 agent 把控制权移交给专家 agent。transcript 会随移交一起传递，
因此专家 agent 能带着完整对话继续处理。

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

也可以把 agent 当作可委派的子程序：

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

子 agent 会在独立循环中运行，最终输出作为工具结果返回。父 agent 不会持有子 agent 的上下文。

## 人类控制

### 工具审批

敏感操作可以加上 `needs_approval=True`。

```python
from lovia import tool


@tool(needs_approval=True)
async def refund(order_id: str, amount_cents: int) -> str:
    """执行退款。"""
    return "refunded"
```

流式模式下，可以在 UI 中处理审批事件：

```python
from lovia import events

handle = Runner.stream(agent, "给订单 A123 退款。")

async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()          # 或 ev.reject()
```

也可以在服务端设置程序化审批策略：

```python
agent = Agent(
    ...,
    approval_handler=lambda call, ctx: "ask" if call.name == "refund" else "allow"
)
```

### 询问人工

`ask_human` 让模型可以通过你的应用向操作员请求输入。

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

## Session 与 Checkpoint

Session 用来跨多次调用保存对话 transcript：

```python
from lovia.stores import SQLiteSession

session = SQLiteSession("chat.db")

await Runner.run(agent, "我的项目叫 Atlas。", session=session, session_id="u1")
result = await Runner.run(agent, "我的项目叫什么？", session=session, session_id="u1")
```

Checkpoint 用来支持长任务的崩溃恢复和幂等运行：

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

两个 SQLite store 都接受 `wal=True`（默认关闭），开启 WAL 日志模式和 busy
timeout——当数据库文件被多个写入方共享时使用，例如多个 store 共用一个文件、
或多进程部署的 web 服务。

这两类 store 都是 **append-only**：`Session` 累积已经结束的 run（每个 run
一个 segment，可以是成功完成的，也可以是调用方主动 finalize 的）；checkpoint
保存的是仍可 resume 的那次 run。因此，完整 transcript 等于 `session.load()`
加上进行中的 snapshot。历史不可变：每次 run 只追加自己的 entries，从不重写。
请为每次 run 提供一个在当前 checkpointer 内唯一的 `run_id`（如 `uuid4().hex`）。
它是 checkpoint 的唯一键，不像 session 那样还受 `session_id` 约束。

对已完成的 `run_id` 重复发起运行会直接重放结果（不再调用模型），并以
`run_id` 为幂等键补写 session——即使先前恰好在 checkpoint 完成与 session
追加之间崩溃，重放也会自动补全会话历史，而不是永久丢失该轮。想区分完整
回答与被 `max_tokens` 截断的回答，可检查 `result.finish_reason`（如
`"stop"` 与 `"length"`）。

## 上下文管理

长对话默认使用 `Compaction`。它只改变“本次发给模型的视图”：完整 transcript
仍保存在 session/checkpoint 中；当上下文窗口吃紧时，它会在模型调用前归档超大的
工具结果、清理较旧的工具结果，并在必要时总结更早的历史。

```python
from lovia import Compaction, Runner

policy = Compaction(
    context_window=200_000,
    compact_at=0.75,
    compact_to=0.50,
)

result = await Runner.run(agent, "继续。", context_policy=policy)
```

`Compaction` 会自动提供 `recall_tool_result` 工具。模型可以凭 `call_id` 找回
被压缩掉的工具结果，无需重新运行工具，也无需手动注册。若要把大体积工具输出归档到
存储中（recall 会从中读回；临时存储会回退到 transcript），给策略传入一个 result store：

```python
from lovia.context import Compaction, FileResultStore

policy = Compaction(context_window=200_000, store=FileResultStore(".cache/results"))
```

如需关闭自动压缩，导入 `from lovia.context import NoopContextPolicy`，并传入
`context_policy=NoopContextPolicy()`。

## 护栏、可靠性与 Hooks

输入和输出护栏都是异步 callable。抛出 `GuardrailTripped`，或返回真值形式的违规消息，
即可中止运行。

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

预算、取消和重试策略都需要显式传入：

```python
from lovia import RetryPolicy, RunBudget

result = await Runner.run(
    agent,
    "分析这些日志。",
    budget=RunBudget(max_tool_calls=20, max_seconds=60),
    retry=RetryPolicy(max_attempts=3),
)
```

生命周期 hooks 接收的正是流式输出使用的同一套类型化事件。每个 handler 都以
`handler(event, ctx)` 的形式被调用：既拿到事件，也拿到本次运行的 `RunContext`
（动态运行状态，包括 `session_id`、当前 agent、累计用量等）：

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

运行中 steering 是取消能力的另一面：取消是让正在运行的 run 停下，steering
则是向它追加一条消息。模型会在下一个 turn 开始时把这条消息当作普通用户消息看到。
`TurnStarted` hook 会在本轮 drain 之前触发，所以它 push 的消息会在当轮生效。
工具和 hooks 都能通过 `ctx.mailbox` 访问同一通道；如果没有传入 `mailbox=`，
runner 会为每个 run 自动创建一个。因此，run 也可以在内部给自己追加指引，
不需要外部接线。

```python
from lovia import Mailbox

# hooks 和 agent 沿用上一段代码。
@hooks.on(events.TurnStarted)
def deadline(ev, ctx: RunContext):
    if ev.turn == 9:
        ctx.mailbox.push("最后一轮：用现有信息直接作答。")


mailbox = Mailbox()
handle = Runner.stream(agent, "分析这些日志。", mailbox=mailbox)
mailbox.push("重点看 14:00 左右的 5xx 峰值。")  # 下一个 turn 生效
```

## 评测

`lovia.eval` 把「agent 的行为对不对」变成一份声明式测试套件。三个概念覆盖全部
API：`Case` 把输入和它必须满足的检查项配成一对；检查项（check）是任意
`(RunResult) -> CheckResult | bool` 的可调用对象（同步异步皆可）——内置匹配器、
LLM 评审和你自己的函数是同一种东西；`evaluate()` 返回 `Report`，可以直接打印、
断言，或与基线做对比。

```python
from lovia.eval import Case, contains, evaluate, llm_judge, tool_called

cases = [
    Case("法国的首都是哪里？", checks=[contains("巴黎")]),
    Case("23.4 * 91 等于多少？", checks=[tool_called("calculator")]),
    Case(
        "写一首关于春天的俳句",
        checks=[llm_judge("符合 5-7-5 音节、意象与春天相关的俳句")],
        samples=4,  # 非确定性靠采样度量，而不是靠重试掩盖：
        pass_threshold=0.75,  # 4 次采样至少 3 次通过才算通过
    ),
]

report = await evaluate(agent, cases)
print(report)
assert report.passed
```

```
eval: 2/3 cases passed (67%) · 6 samples · 4,812 tokens · 21.4s
  ✓ 法国的首都是哪里？        1/1
  ✓ 23.4 * 91 等于多少？      1/1
  ✗ 写一首关于春天的俳句      2/4  llm_judge (score 0.55) — 第三行有八个音节
```

让套件既诚实又省钱的细节：

- **任何函数都是检查项。** `lambda r: r.turns <= 3` 就能用。内置：`contains` /
  `not_contains`、`regex`、`equals`、`matches`（结构化输出的子集匹配）、
  `tool_called` / `tool_not_called`、`max_turns`、`max_tokens`、`no_error`，
  并可用 `all_of` / `any_of` / `weighted` 组合。
- **`llm_judge(rubric)`** 用模型给语义打分（默认读
  `$LOVIA_EVAL_JUDGE_MODEL`），它也只是一个普通检查项——把
  `lovia.testing.ScriptedProvider` 作为它的 `model`，整个套件即可离线运行。
- **错误也是数据。** 某次采样抛异常只记为该样本的 `error` 并单独判负；
  一个坏 case 或坏检查项不会中断整个套件。
- **基线对比。** `report.save(path)`、`Report.load(path)` 加上
  `current.compare(baseline)`，在 CI 里直接标出回归与改进。

完整的离线脚本化套件见 `examples/28_eval.py`。

## 内置工具

lovia 不会自动往 agent 里塞工具；需要哪些工具，由你自己选择。

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

如果已有自己的搜索后端，实现 `WebSearch` 并传给 `web_search()` 即可。

## 插件

**Plugin** 是 lovia 唯一的扩展轴：把一项能力打包成一个对象。

单个插件可以贡献任意组合：`tools`、系统提示 `instructions`、每轮注入的
`view_injectors`（临时提醒，永不写入 transcript）、事件 `hooks`，以及
`input_guardrails` / `output_guardrails`。

runner 会在**每次 run**（以及 handoff 后的每个 agent）await 插件的异步
`setup()` 来激活它；run 结束时，再通过 `aclose()` 释放它打开的资源。

插件只做增量贡献，不驱动控制流；中止、重试和 handoff 始终由 loop 掌控。
下面的 Skills、MCP 和 todo 列表都是内置插件。

### Todo 列表

内置 todo 插件会给模型一个清单工具，并在每一轮重新展示当前清单，同时不会让持久化的
transcript 膨胀：

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

Skills 是遵循 Agent Skills 规范的可复用指令包。lovia 会先暴露轻量 metadata，
让模型判断是否需要；完整指令和引用文件只在需要时加载。

```python
from lovia import Agent, Skills

agent = Agent(
    name="support",
    instructions="根据正确政策帮助客户。",
    model="deepseek-v4-pro",
    plugins=[Skills("./skills")],
)
```

一个 skill 目录包含带 YAML frontmatter 的 `SKILL.md`，也可以包含 `references/`、
`scripts/` 和 `assets/`。你可以传入多个目录，也可以用 filter 控制哪些 skill
暴露给模型：

```python
plugins=[Skills("./skills", "./team-skills")]
plugins=[Skills("./skills", filter=lambda meta: "internal" not in meta.extra.get("tags", []))]
```

如需自定义后端，把 `SkillSource`（或预先构建好的 `SkillCategory`）传给
`Skills()`，而不是传路径。

### MCP

[Model Context Protocol](https://modelcontextprotocol.io) server 可以把自己的工具暴露给
agent。先安装可选依赖：

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

默认情况下，每次 run 都会打开并关闭 server。若要跨多次 run 复用同一连接，
可以打开一个 session，并把活跃连接传进去：

```python
server = MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])

async with server.session() as conn:
    agent = Agent(name="assistant", model="deepseek-v4-pro", plugins=[MCP(conn)])
    await Runner.run(agent, "抓取 https://example.com 并总结。")
```

`MCP()` 可以接收多个 server，例如 `MCP(a, b)`；`MCPServer.name` 会给对应 server
的工具名加前缀（如 `web__fetch`），避免命名冲突。

### 长期记忆

`Memory` 为 agent 提供可跨 run、跨 session 保留的长期记忆。它分为两个层级，
并暴露三个模型很容易理解的动作：

- **Notes**（*热*层）是一小段有字符预算的笔记，**每次都会注入**系统提示，
  用来保存用户的稳定偏好和长期事实。模型可以通过 `remember(fact)` /
  `forget(fact)` 主动维护；默认情况下，插件也会在 run 结束时自动提取值得长期保留的事实，
  写入 Notes。
- **Archive**（*冷*层）是支持全文检索的历史对话归档。它不会默认进入上下文，
  只在模型调用 `recall(query)` 时按需取回相关内容。

```python
from lovia import Agent, Memory

agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    plugins=[Memory("./.lovia/memory")],
)
```

召回质量可以逐级升级；每升一级，只需多传一个参数：

```python
Memory("./memory")                             # 标准库关键词检索（FTS5 bm25）
Memory("./memory", embedder=OpenAIEmbedder())  # + 语义臂 → 混合召回
Memory("./memory", index=my_index)             # 使用你自己的检索引擎
```

- **零配置** 默认使用标准库 SQLite FTS5（bm25 + CJK 感知的 bigram 索引）。
  纯词法检索的短板由 agent 本来就有的 LLM 补上：`recall` 查询会在检索前扩展出同义词和跨语言翻译（`expand_query="auto"`）。
  run 结束时的一次整理调用会把长期事实写入 Notes，并为 Archive 生成一段自包含的对话摘要。
  这类摘要比零散聊天片段更容易被检索到。
- **`embedder=`** 会把默认索引升级为关键词 + 向量的混合检索，并用 Reciprocal Rank
  Fusion 融合结果，在不增加新依赖的情况下获得语义和跨语言召回能力。向量存放在
  SQLite 中；`OpenAIEmbedder` 兼容任何 OpenAI 风格的 `/embeddings` 端点，例如官方
  API、SiliconFlow 上的 BGE-M3、DashScope 或本地服务。`OPENAI_EMBEDDING_BASE_URL` /
  `OPENAI_EMBEDDING_API_KEY` 可以独立于 chat 端点配置，因为聊天和 embedding
  常常在不同服务商上。此时查询扩展会自动关闭，因为语义召回已经覆盖了它的收益。
- **`index=`** 会直接替换整个检索引擎。`Index` 只有三个方法（`add` / `remove` /
  `search`，按 `Doc.id` upsert），可以基于 Elasticsearch、向量数据库或其他检索引擎实现。
  多个检索臂可用 `|` 组合：`KeywordIndex(...) | VectorIndex(...) | my_arm`
  就是一个经 RRF 融合的混合索引。传入 `index=None` 会关闭冷层和 `recall` 工具。

默认实现会把文件放在你传入的根目录下：

```
.lovia/memory/
├── MEMORY.md      # 热层：一行一条长期事实，始终放进上下文，可手工编辑
├── archive.db     # 冷层：历史对话的关键词索引
└── vectors.db     # 冷层：向量臂（仅在传入 embedder= 时创建）
```

> **隐私。** Archive 会把用户和助手的消息文本持久化到磁盘，因此可能保存敏感内容。
> 请把记忆目录放在访问控制合适的位置；如果不希望保留可检索的历史对话记录，请传入
> `index=None`。

可以用可选参数调整行为：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `auto_curate` | `True` | run 结束时的一次整理调用：长期事实 → Notes，对话摘要 → Archive；Notes 超预算时合并整理 |
| `expand_query` | `"auto"` | 用 LLM 为 `recall` 查询扩展同义词/翻译；`"auto"` = 仅在默认纯词法索引下开启 |
| `summarize_recall` | `True` | `recall` 返回由模型整理过的命中摘要，而不是原始片段 |
| `recall_k` | `5` | `recall` 取回的命中数量 |
| `notes_budget` | `2000` | Notes 的字符预算，也是提示词中的容量表和整理触发线 |
| `model` | 宿主模型 | 用于整理、合并、查询扩展和召回摘要的模型 |

整理、合并和召回摘要这些内部请求，会通过一个没有工具、没有插件的子 agent
调用 `Runner.run`，并使用结构化输出。因此它们可以复用同一条 provider 链，
又不会递归触发 `Memory` 自身。lovia 会完整保留 transcript，context compaction
只影响传给模型的视图，所以整理只需要在 run 结束时针对完整 transcript 跑一次：
它做的是整理，把少量长期事实放进小而稳定的热层，而不是在上下文丢失后再补救。

`remember` / `forget` 同时也是公开方法（如 `await mem.remember("...")`），
代码可以在没有模型参与的情况下预置或清理 Notes。

**自带后端。** 两个层级背后各有一个刻意收窄的协议。`NotesStore` 只有两个方法：
`load` / `save` 一组事实，归一化、去重和预算控制都留在插件里；`Index` 就是上面提到的
三方法检索接口，除 `Doc` / `Hit` 外不涉及任何 lovia 类型。Doc id 是确定性的
（`run_id:seq`），因此 resumed run 重新写入时会按 id upsert，不会产生重复记录。
后端只需正确实现“按 id 覆盖”即可：

```python
from lovia import Agent, Memory

agent = Agent(name="assistant", plugins=[Memory(notes=my_notes, index=my_index)])
```

自定义后端是长生命周期对象，会被每次 run 共享，因此需要保证并发安全；插件不会替你关闭它们。

### 编写插件

一个插件可以是任意对象，只要它带有 `name`，并提供返回 `PluginInstance` 的
`async setup()`。

需要**每次 run 全新创建**的状态，放在 `setup` 内部（如上面的 todo 列表）；
需要**跨 run、跨 session 持久化**的状态，则挂在插件对象上，在构造时传入。

下面是一个术语表插件：它包裹一个由你提供的后端。这个后端只创建一次，并被每次 run
共享，所以一次对话里定义的术语，在下一次对话里仍然可查。（上面的内置 `Memory`
插件正是基于这个模式。）

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

    store: Glossary  # 长生命周期，被每次 run 共享；不会在每次 run 重建
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


store = MyGlossary()  # 你的 Glossary 后端：只需提供异步的 define() 和 lookup()
agent = Agent(name="assistant", model="deepseek-v4-pro", plugins=[GlossaryPlugin(store)])
```

由于这个后端会被多个 run 共享，而且这些 run 可能并发执行，所以它必须支持并发访问。
插件也不会关闭它；它的生命周期属于创建它的人。（对比 todo 插件：它的 store 会在每次
run 的 `setup` 里重建。）

`PluginInstance` 可携带以下贡献的任意子集：

| 字段 | 作用 |
| --- | --- |
| `tools` | 合并到 agent 的工具集 |
| `instructions` | 追加到系统提示 |
| `view_injectors` | 每轮追加到模型视图的条目——永不持久化 |
| `hooks` | 观察 run 事件的 `AgentHooks`，用于指标、审计等场景 |
| `input_guardrails` / `output_guardrails` | 在 loop 的检查点运行，与 agent 自身的护栏一起生效；中止仍由 loop 掌控 |
| `aclose` | run 结束时 await，用于释放 `setup` 中打开的资源 |

## Workspace Agents

`Workspace` 会为 agent 增加文件和 shell 工具，并用根目录和权限策略限制它们的作用范围。

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
        readable=("~/reference-docs",),   # 根目录之外的额外可读范围
        denied_paths=(".env*",),
        command_rules=(
            CommandRule("pytest", "allow"),
            CommandRule("rm -rf", "deny"),
        ),
    ),
)
```

文件与 shell 命令共享同一套 `allow` / `ask` / `deny` 策略。路径可以是相对
workspace 的，也可以是绝对路径；符号链接按其解析目标判定 —— 指向系统解释器的
`.venv/bin/python` 在策略允许时可以直接读取。`ask` 决策与 shell 命令走同一个
审批通道。

模式：

| 模式 | 根目录内 | 根目录外 | shell 默认 |
| --- | --- | --- | --- |
| `readonly` | 只读 | 拒绝 | 无 shell |
| `coding` | 读 + 写 | 读需审批，写拒绝 | 审批 |
| `trusted` | 读 + 写 | 读允许，写需审批 | 允许 |

用 `readable=` / `writable=`（或完整的 `path_rules=`）扩大范围；用
`denied_paths` 封禁路径 —— 被封禁的路径不仅文件工具拒绝，点名它们的 shell
命令（包括重定向目标）同样会被拒绝。命令级路径守卫是词法层面的建议性防护 ——
本地 shell 仍以宿主机用户身份运行；如果需要强隔离，请使用 `ShellExecutor`
接缝（OS 级沙箱）或未来的容器后端。

## Web UI

可选的 Web 层是一个轻量 FastAPI 应用，提供 SSE 流式输出、session、Markdown 渲染和审批路由。

```bash
pip install "lovia[web]"
```

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

### 命令行启动

无需写代码：`python -m lovia.web` 会构建一个默认 agent，然后启动聊天 UI。这个默认
agent 会从环境变量读取模型，从 `./skills` 加载技能，把长期记忆存到 `./.lovia/memory`，
带上一个 todo 清单，启用模型驱动的定时运行（agent 可以在你批准后安排自己的后续任务），
内置时间、HTTP 抓取和网页搜索工具，并在当前目录开启一个 trusted workspace。

```bash
python -m lovia.web                                    # 零配置
python -m lovia.web --port 9000 --model deepseek-v4-pro
python -m lovia.web --skills-dir ./skills --workspace-mode readonly
python -m lovia.web --memory-dir ./mem                 # 记忆存到 ./mem
python -m lovia.web --app myagents:assistant           # 启动你自己的 Agent
```

常用选项也可以通过 `LOVIA_*` 环境变量指定（优先级：**命令行 > 环境变量 > 默认值**）。
如果安装了 `python-dotenv`，当前目录下的 `.env` 会被自动加载；也可以用 `--env-file`
显式指定。模型凭证沿用各 provider 自己的 `OPENAI_API_KEY` / `OPENAI_BASE_URL`
（Anthropic 使用 `ANTHROPIC_*`）。

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

`--provider-timeout` 和 `--trust-env` 由 provider 直接读取，因此对 `--app` agent
和库调用同样生效。`--max-retries` / `--max-turns` 作用于每次被服务的 run；
`--max-tokens` / `--context-window` 只用于配置默认 agent。

在内网 CA 场景下，`LOVIA_HTTP_CA_BUNDLE` 会让所有出站 HTTPS（模型 provider 和
`http_fetch` 工具）使用指定的 PEM 证书包；`LOVIA_HTTP_INSECURE=1` 会关闭证书校验
（仅应在可信网络中使用）。`web` extra 已包含 `truststore`，会自动信任操作系统证书库：
浏览器信任的证书，lovia 也会信任，无需额外配置。

默认 agent 还会带上一组常用能力：`todo_write` 清单，以及 `now`（时间）、
`http_fetch`、`web_search` 工具。网页搜索需要 `ddg` extra（已包含在 `lovia[web]`
中）；如果缺少该依赖，只会跳过这个工具。

`--version` 打印版本号；完整选项见 `python -m lovia.web --help`。

### 用 API 自建 UI

HTTP API 与内置聊天页面相互解耦。你可以保留 JSON + SSE 接口，同时换上自己的前端。
可以直接关掉内置 UI：

```python
from lovia.web import create_app

app = create_app(agent, ui=False)   # 不挂载 GET / 与 /static，只暴露 API
```

也可以把这套不带 UI 的 router 挂进你自己的 FastAPI 应用：

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

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/info` | server 标题、agents、版本、能力开关 |
| `GET /api/agents`、`GET /api/agents/{name}` | 列出 / 获取 agent |
| `POST /api/chat` | 阻塞式单轮对话，返回 `{output, session_id, usage}` |
| `POST /api/chat/stream` | 流式单轮对话，通过 SSE 返回增量事件（`text_delta`、`tool_call`、`done` 等） |
| `POST /api/chat/approve`、`POST /api/chat/cancel` | 处理审批 / 停止流式响应 |
| `GET /api/sessions` | 会话列表（`?q=` 搜索、`?limit=` 限制数量）；`DELETE` 清空全部 |
| `GET`/`PATCH`/`DELETE /api/sessions/{id}` | 查看 / 重命名 / 删除会话 |
| `GET /api/sessions/{id}/export?format=md\|json\|txt` | 导出会话 |
| `GET`/`POST /api/schedules`、`DELETE`/`PATCH /api/schedules/{id}` | 定时任务列表 / 创建 / 删除 / 暂停（cron · 间隔 · 定时） |

`lovia/web/static/js/api.js` 是一个开箱即用的浏览器客户端（含 SSE 读取器）——
可以直接 import，也可以把它当作其他语言客户端的参考实现。

## 示例

`examples/` 目录是一条按编号排列的学习路径，每个脚本都自包含、可直接运行——
`cp .env.example .env`，设好 `LOVIA_MODEL`，从 `01_hello.py` 开始。完整索引和
配置说明见 [examples/README-zh.md](examples/README-zh.md)。

| 分组 | 文件 | 覆盖内容 |
| --- | --- | --- |
| 基础 | `01`–`06` | hello、工具、流式、结构化输出、会话、多模态 |
| 多 agent | `07`–`08` | handoff、agent 即工具 |
| 模型与 provider | `09`–`10` | `ModelSettings`、兼容端点、自定义 `Provider`（离线可跑） |
| 控制与生产 | `11`–`18` | hooks、审批、护栏、可靠性、断点恢复、steering、上下文压缩、依赖注入 |
| 工作区与插件 | `19`–`25` | 工作区、编码 agent、todo、skills、记忆、MCP、自写插件 |
| 服务与应用 | `26`–`30` | Web UI、JSON/SSE API、评测、数据分析、终端客服 bot |
| `examples/tools/` | | 每个内置工具族一个脚本 |
| `examples/workflows/` | | prompt chaining、routing、parallelization、orchestrator-workers、evaluator loop、自主 agent |

## 安装可选依赖

| 需求 | 安装 |
| --- | --- |
| 核心框架 | `pip install lovia` |
| DuckDuckGo 搜索 | `pip install "lovia[ddg]"` |
| MCP 集成 | `pip install "lovia[mcp]"` |
| Web UI | `pip install "lovia[web]"` |
| 运行示例 | `pip install "lovia[examples,web]"` |
| 开发、测试、发布 | `pip install -e ".[dev]"` |

`examples` 包含运行演示脚本所需的依赖，例如 `python-dotenv`、`rich` 和 `ddgs`。
`dev` 包含维护仓库所需的依赖，例如 `pytest`、`ruff`、`mypy`、`build`、`twine`
以及 Web 测试栈。二者刻意分开，避免普通开发时安装只服务于演示的依赖。

## 开发

```bash
pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format .
.venv/bin/python -m mypy lovia
```

`examples/` 目录里有覆盖主要能力的可运行脚本。真实 provider 的端到端测试带有
`live_provider` 标记，默认不会运行。
