# lovia

[English](./README.md) · [文档](./docs/zh/README.md) · [示例](./examples/README-zh.md)

lovia 是一个优雅、克制的 Python agent 框架，写给想要**自己掌控 agent 循环**、
又不愿从零重造每个配套原语的开发者。它提供 agent 应用迟早需要的那些部件——
工具、流式输出、结构化输出、会话、handoff、审批、护栏、工作区、技能、MCP、
上下文压缩、检查点/恢复，以及一个小巧的 web UI——同时让内核保持足够直接，
可以读懂、替换和扩展。

核心抽象只有几个：

- `Agent` 是不可变的配置；
- `Runner` 执行一次运行；
- `@tool` 就是一个带类型标注的 Python 函数；
- `Handoff` 与 `agent.as_tool()` 是组合 agent 的两个原子方式；
- 插件（plugin）打包可复用能力，但从不接管控制流。MCP、技能、待办清单、
  长期记忆都可以表达为插件。

这就是取舍所在：解决 agent 应用反复出现的难点，但避免让框架变成平台。

```bash
pip install lovia
```

```python
from lovia import Agent, Skills, Todo, tool
from lovia.workspace import Workspace


@tool
def lookup_ticket(ticket_id: str) -> str:
    """Look up an internal support ticket."""
    return f"{ticket_id}: waiting for customer reply"


agent = Agent(
    name="operator",
    instructions=(
        "You are a customer-support operator. "
        "Before replying, confirm the ticket state, then use team policy "
        "to give a clear, restrained, actionable response."
    ),
    model="deepseek-v4-pro",
    tools=[lookup_ticket],
    plugins=[Todo(), Skills("./skills")],
    workspace=Workspace.local(".", mode="trusted"),
)

# run_sync() 免去脚本和 notebook 里的 asyncio 样板；
# 异步代码中请使用 `await Runner.run(agent, ...)`。
result = agent.run_sync(
    "Check ticket T-1001 and draft a reply using our team guidelines.",
)
print(result.output)
```

这里的 `./skills` 指向你团队的技能目录；还没有的话，先去掉
`Skills("./skills")`。使用 OpenAI 官方端点时设置 `OPENAI_API_KEY`；
DeepSeek、Ollama、vLLM 等 OpenAI 兼容服务则设置 `OPENAI_BASE_URL`。
Anthropic 也是内置的：`model="anthropic:claude-4-8-opus"`。

或者完全不写代码——零配置的 playground 会启动一个带记忆、技能、定时任务
和当前目录工作区的聊天 UI：

```bash
pip install "lovia[web]"
python -m lovia.web
```

## 文档

本 README 是一次巡礼。[文档](./docs/en/README.md)按功能逐页深入
（中文翻译[进行中](./docs/zh/README.md)）——从
[快速上手](./docs/en/quickstart.md)和[核心概念](./docs/en/concepts.md)
开始；[示例](./examples/README-zh.md)是一条编号排列、可直接运行的学习路径。

## 为什么是 lovia

lovia 偏爱可组合的原语，而不是再造一套新的抽象宇宙。它贴近普通的 Python：
dataclass、protocol、async 函数、显式组合。

- **可读。** `lovia/runner.py` 是一层门面；可变的运行状态都在
  `lovia/runtime/loop.py`。当行为让你意外时，穿过代码的路径很短。
- **供应商中立，且没有适配器税。** 内置 provider 直接通过 `httpx` 说
  OpenAI Chat Completions 和 Anthropic Messages。自定义 provider 是一个
  `Protocol`，不是一场子类化工程。
- **上下文管理可替换。** 默认的 `Compaction` 只改变模型下一次调用看到的
  内容。会话和检查点保留完整转录，高级用户可以提供自己的 `ContextPolicy`。
- **多 agent 组合保持原子。** Handoff 把控制权移交给专家 agent；
  agent-as-tool 委派一个有边界的子任务。两者都是原语，而不是一套
  必须整体采纳的编排 DSL。
- **有生产接缝，而不是生产戏服。** 审批、预算、取消、运行中转向、重试、
  钩子、受限的工作区工具、检查点/恢复，都是可以接进你自己应用的显式旋钮。
- **只有一条扩展轴。** 插件把工具、提示词补充、每轮视图注入、钩子、护栏
  和清理打包在一起。技能、MCP、待办清单、长期记忆用的都是同一套机制。

## 从小处开始，按需添加

你可以把 lovia 当作模型调用的一层薄封装，等产品提出要求时再加能力。

| 当你需要…… | 添加…… | 指南 |
| --- | --- | --- |
| 快速脚本或 notebook 助手 | `Agent.run_sync(...)` | [Running](./docs/en/running.md) |
| 工具调用 | `@tool` 函数（默认并行） | [Tools](./docs/en/tools.md) |
| 副作用不能交叠的工具 | `@tool(parallel=False)` | [Tools](./docs/en/tools.md) |
| 带类型的最终答案 | `output_type=YourModel` | [Structured output](./docs/en/structured-output.md) |
| 实时 UI 更新 | `Runner.stream(...)` 与类型化事件 | [Streaming](./docs/en/streaming.md) |
| 多轮对话 | `SQLiteSession` 或自定义 `Session` | [Sessions](./docs/en/sessions-and-checkpoints.md) |
| 崩溃恢复、幂等运行 | `CheckpointOptions` | [Checkpoints](./docs/en/sessions-and-checkpoints.md#checkpoints) |
| 多 agent 路由或委派 | `handoffs=[...]` 或 `agent.as_tool()` | [Multi-agent](./docs/en/multi-agent.md) |
| 人工审批 | `@tool(needs_approval=True)` | [Human in the loop](./docs/en/human-in-the-loop.md) |
| 文件与 shell 命令 | `Workspace.local(...)` | [Workspace](./docs/en/workspace.md) |
| 长上下文存活 | `Compaction`（自动提供 recall） | [Context](./docs/en/context.md) |
| 跨会话记忆 | `Memory(...)` | [Memory](./docs/en/memory.md) |
| 可复用能力 | `PluginInstance`、`Skills`、`Todo` 或 `MCP` | [Plugins](./docs/en/plugins.md) |
| 行为测试套件 | `lovia.eval` | [Evals](./docs/en/eval.md) |

## 设计哲学

lovia 按顺序优化四件事，顺序本身就是态度。

1. **简明。** 一个功能应当装得进脑子。公开接口应当一目了然，需要调试时
   内部实现应当读得下去。
2. **轻量。** 内核应当导入快、安装干净，不夹带你没要的基础设施。
3. **可扩展。** 真实应用需要自己的 provider、存储、策略、工具和 UI。
   lovia 给的是接缝，不是锁定。
4. **通用。** 内置件实用而不神奇——它们只是同一套扩展点的示范，你自己
   也能用。

设计的压力来自克制：如果一个功能可以是一段简短的用户侧配方，它就不该成为
框架的表面积；如果它属于框架，它应当与现有循环组合，而不是另起一个循环。

## 各部件如何协作

每次运行都遵循同一个形状：

```text
Agent + input
  -> RunLoop 加载会话/检查点状态
  -> 插件贡献工具、指令、钩子、护栏、视图注入器
  -> 上下文策略渲染本次调用的模型视图
  -> provider 流式返回类型化增量
  -> 工具、审批、handoff、护栏、钩子在显式检查点运行
  -> 本次运行自己的条目追加进会话；运行被写入检查点
```

让这一切可预测的边界——转录 vs 视图、会话 vs 检查点、姿态 vs 限额——
见[核心概念](./docs/en/concepts.md)。

## 巡礼

以下每一站都有完整指南；每段代码都可按原样运行。

### Agent

`Agent` 是声明式配置——不含会话状态，可安全共享、廉价 `clone()`。
提示词片段可以按运行动态生成：

```python
from lovia import Agent

agent = Agent(name="writer", instructions="Write concrete, concise answers.",
              model="deepseek-v4-pro")

@agent.instruction
async def user_tier(ctx) -> str:
    return f"User tier: {ctx.deps['tier']}"
```

→ [Agents](./docs/en/agents.md)

### 运行与流式

一次运行，三种消费方式——流式句柄既可异步迭代又可 await。迭代永不抛错：
每个流以恰好一个终止事件收尾。

```python
from lovia import Runner, events

handle = Runner.stream(agent, "Explain context windows in one paragraph.")

async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

→ [Running](./docs/en/running.md) · [Streaming](./docs/en/streaming.md)

### 工具

带类型的 Python 函数；schema 来自签名、docstring、`Annotated` 与 Pydantic
`Field`。同一轮的多个调用默认并发执行——副作用不可重入的工具选择退出，
成为执行屏障：

```python
from typing import Annotated
from pydantic import Field
from lovia import tool


@tool(strict=True)
def search_docs(
    query: Annotated[str, "Search terms"],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """Search internal documentation."""
    return []


@tool(parallel=False)
async def apply_migration(name: str) -> str:
    """Apply a database migration (never concurrently with other tools)."""
    return "applied"
```

→ [Tools](./docs/en/tools.md) · [Built-in tools](./docs/en/built-in-tools.md)

### 结构化输出

传入 Pydantic 模型、dataclass、`TypedDict` 或普通类型；最终答案经过校验——
解析失败时默认自动修复一次：

```python
from pydantic import BaseModel
from lovia import Agent, Runner


class Brief(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(name="summarizer", model="deepseek-v4-pro", output_type=Brief)
result = await Runner.run(agent, "Summarize lovia for a Python developer.")
print(result.output.title)
```

→ [Structured output](./docs/en/structured-output.md)

### Provider

模型字符串、provider 实例或 fallback 链；OpenAI 兼容端点走
`OPENAI_BASE_URL`，提示缓存与推理模型按 host 处理，自定义 provider
只是一个小 `Protocol`：

```python
from lovia import Agent, ModelSettings, model_from_env

agent = Agent(
    name="assistant",
    model=["anthropic:claude-4-8-opus", "deepseek-v4-pro"],  # fallback 链
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)

scripted = Agent(name="ci", model=model_from_env())  # LOVIA_MODEL，缺失则大声报错
```

→ [Providers & models](./docs/en/providers.md)

### 多 Agent

两个原语，底层都是普通工具。Handoff 把对话移交给专家；agent-as-tool
委派一个有边界的子任务：

```python
from lovia import Agent, Runner

billing = Agent(name="billing", instructions="Handle billing issues.", model="deepseek-v4-pro")
support = Agent(name="support", instructions="Handle technical issues.", model="deepseek-v4-pro")

triage = Agent(
    name="triage",
    instructions="Route the user to the right specialist.",
    model="deepseek-v4-pro",
    handoffs=[billing, support],
    tools=[support.as_tool(description="Ask the tech specialist a question.")],
)

result = await Runner.run(triage, "I was charged twice.")
```

→ [Multi-agent](./docs/en/multi-agent.md)

### 人工介入

给敏感工具加门禁；在 UI、服务端策略或带外通道里裁决——无人裁决即拒绝，
运行永不悬挂：

```python
from lovia import Runner, events, tool


@tool(needs_approval=True)
async def refund(order_id: str, amount_cents: int) -> str:
    """Issue a refund."""
    return "refunded"


async for ev in Runner.stream(agent, "Refund order A123."):
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()          # 或 ev.reject()
```

→ [Human in the loop](./docs/en/human-in-the-loop.md)

### 会话与检查点

会话让对话跨运行持续；检查点让单次运行可崩溃恢复且幂等——重发一个已完成的
`run_id` 会直接重放结果，不再调用模型：

```python
from lovia import CheckpointOptions, Runner, SQLiteCheckpointer, SQLiteSession

session = SQLiteSession("chat.db")
await Runner.run(agent, "My project is called Atlas.", session=session, session_id="u1")

cp = SQLiteCheckpointer("runs.db")
result = await Runner.run(
    agent,
    "Migrate the report format.",
    checkpoint=CheckpointOptions(cp, "report-migration-42"),
)
```

→ [Sessions & checkpoints](./docs/en/sessions-and-checkpoints.md)

### 上下文压缩

长对话在不改写历史的前提下活过上下文窗口：压缩只作用于视图、保持提示词
前缀稳定以命中 provider 缓存，并自动提供 `recall_tool_result` 让模型取回
被视图丢弃的内容：

```python
from lovia import Agent, Compaction

agent = Agent(
    name="companion",
    model="deepseek-v4-pro",
    context_policy=Compaction(context_window=200_000, compact_at=0.75, compact_to=0.50),
)
```

→ [Context management](./docs/en/context.md)

### 护栏与可靠性

护栏在输入/输出边界拥有否决权。可靠性遵循一条放置规则——重试姿态放在
agent 上，单次限额放在运行上：

```python
from lovia import Agent, RetryPolicy, RunBudget, Runner
from lovia.exceptions import GuardrailTripped


async def must_cite(output, ctx):
    if "source:" not in str(output).lower():
        return "Missing source citation."


agent = Agent(name="researcher", model="deepseek-v4-pro",
              output_guardrails=[must_cite],
              retry=RetryPolicy(max_attempts=2))            # 姿态

result = await Runner.run(agent, "Analyze these logs.",
                          budget=RunBudget(max_tool_calls=20, max_seconds=60))  # 限额
```

→ [Guardrails](./docs/en/guardrails.md) · [Reliability](./docs/en/reliability.md)

### 钩子与转向

钩子观察每个运行事件（fail-open，与流式同一套类型）；mailbox 是取消的
入方向对偶——向运行中的 agent 推送消息，模型下一轮就能看到。运行甚至可以
给自己转向：

```python
from lovia import Mailbox, RunContext, Runner, events
from lovia.hooks import AgentHooks

hooks = AgentHooks()


@hooks.on(events.TurnStarted)
def deadline(ev, ctx: RunContext):
    if ev.turn == 9:
        ctx.mailbox.push("Last turn: answer with what you have.")


mailbox = Mailbox()
handle = Runner.stream(agent.clone(hooks=hooks), "Analyze these logs.", mailbox=mailbox)
mailbox.push("Focus on the 5xx spike around 14:00.")  # 下一轮生效
```

→ [Observability](./docs/en/observability.md) · [Reliability](./docs/en/reliability.md#steering-a-live-run)

### 工作区

作用域限定在根目录内的文件与 shell 工具，由一套 `allow`/`ask`/`deny`
策略同时治理路径**和**命令——`ask` 走标准审批通道：

```python
from lovia import Agent
from lovia.workspace import CommandRule, Workspace

agent = Agent(
    name="coder",
    instructions="Make small, targeted code changes.",
    model="deepseek-v4-pro",
    workspace=Workspace.local(
        ".",
        mode="coding",
        denied_paths=(".env*",),
        command_rules=(CommandRule("pytest", "allow"), CommandRule("rm -rf", "deny")),
    ),
)
```

→ [Workspace](./docs/en/workspace.md)

### 插件

唯一的扩展轴：一个插件可以贡献工具、提示词、每轮视图注入器、钩子和护栏——
但从不接管控制流。Todo、Skills、MCP 都是插件：

```python
from lovia import Agent, Skills, Todo
from lovia.plugins.mcp import MCP, MCPServerStdio

agent = Agent(
    name="builder",
    model="deepseek-v4-pro",
    plugins=[
        Todo(),                      # 外化的待办清单，每轮重新展示
        Skills("./skills"),          # 按需加载的指令包
        MCP(MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])),
    ],
)
```

→ [Plugins](./docs/en/plugins.md) · [Skills](./docs/en/skills.md) · [MCP](./docs/en/mcp.md)

### 记忆

两层结构、三个动词构成长期记忆：始终在提示词里的字符预算 **Notes**
（`remember`/`forget`），按需检索的历史对话 **Archive**（`recall`）——
零配置即 SQLite FTS5，一次加一个参数即可升级：

```python
from lovia import Agent, Memory
from lovia.plugins import OpenAIEmbedder

agent = Agent(name="assistant", model="deepseek-v4-pro",
              plugins=[Memory("./.lovia/memory")])

Memory("./memory")                             # 标准库关键词检索（FTS5 bm25）
Memory("./memory", embedder=OpenAIEmbedder())  # + 语义臂 -> 混合检索
Memory("./memory", index=None)                 # 只留 Notes，不建档案
```

→ [Memory](./docs/en/memory.md)

### Web UI

一个小巧的 FastAPI 应用——SSE 流式、带标题的会话、审批、定时任务、
记忆编辑器——运行在浏览器断开后继续存活。JSON + SSE API 可独立使用，
接你自己的前端：

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

```bash
python -m lovia.web --port 9000 --model deepseek-v4-pro   # 或零配置直接启动
```

→ [Web UI & server](./docs/en/web.md) · [HTTP API](./docs/en/http-api.md)

### 评测

行为测试套件：`Case` 把输入和检查配对，任何函数都是检查，LLM 裁判也只是
另一个检查，报告可与基线对比接入 CI：

```python
from lovia.eval import Case, contains, evaluate, llm_judge, tool_called

report = await evaluate(agent, [
    Case("What is the capital of France?", checks=[contains("Paris")]),
    Case("What's 23.4 * 91?", checks=[tool_called("calculator")]),
    Case("Write a haiku about spring",
         checks=[llm_judge("A 5-7-5 haiku that evokes spring")],
         samples=4, pass_threshold=0.75),
])
print(report)
assert report.passed
```

→ [Evals](./docs/en/eval.md)

### 测试

一切都可以离线跑在脚本化 provider 上——真实工具、真实循环、预置的模型回复：

```python
from lovia.testing import ScriptedProvider, call, text

provider = ScriptedProvider([
    call("add", {"a": 2, "b": 3}, call_id="c1"),
    text("The answer is 5."),
])
```

→ [Testing](./docs/en/testing.md)

## 示例

`examples/` 目录是一条编号排列、自包含、可直接运行的学习路径——
`cp .env.example .env`、设置 `LOVIA_MODEL`，从 `01_hello.py` 开始。完整索引
与环境说明见 [examples/README-zh.md](examples/README-zh.md)。

| 分区 | 文件 | 覆盖内容 |
| --- | --- | --- |
| 基础 | `01`–`06` | hello、工具、流式、结构化输出、会话、多模态 |
| 多 agent | `07`–`08` | handoff、agent-as-tool |
| 模型与 provider | `09`–`10` | `ModelSettings`、兼容端点、自定义 `Provider`（离线） |
| 控制与生产 | `11`–`18` | 钩子、审批、护栏、可靠性、恢复、转向、压缩、依赖注入 |
| 工作区与插件 | `19`–`25` | 工作区、编码 agent、待办、技能、记忆、MCP、自写插件 |
| 服务与应用 | `26`–`30` | web UI、JSON/SSE API、评测、数据分析、终端客服 bot |
| `examples/tools/` | | 每个内置工具族一个脚本 |
| `examples/workflows/` | | 提示链、路由、并行化、编排者-工作者、评估循环、自主 agent |

## 安装选项

| 需求 | 安装 |
| --- | --- |
| 核心框架 | `pip install lovia` |
| DuckDuckGo 搜索 | `pip install "lovia[ddg]"` |
| MCP 集成 | `pip install "lovia[mcp]"` |
| Web UI | `pip install "lovia[web]"` |
| 可运行示例 | `pip install "lovia[examples,web]"` |
| 开发 | `pip install -e ".[dev]"` |

## 开发

```bash
pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format .
.venv/bin/python -m mypy lovia
```

标记为 `live_provider` 的真实端点测试默认跳过，需显式开启。面向贡献者的
内部机制文档见 [docs/architecture.md](docs/architecture.md)。
