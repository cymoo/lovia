# lovia

[English](./README.md) | **简体中文**

lovia 是一个优雅、克制的 Python agent 框架，适合想要掌控 agent
循环、又不想从零搭完所有基础设施的开发者。它把 agent 应用里反复出现的
难点处理好：工具、会话、事件流、上下文压缩、服务化等；但不会因此变得大而重。

```bash
pip install lovia
```

```python
from lovia import Agent, tool


@tool
def get_order(order_id: str) -> str:
    """根据订单号查询订单状态。"""
    return f"订单 {order_id}：两天前已发货，预计周四送达。"


# 在环境变量里配置 OPENAI_BASE_URL 和 OPENAI_API_KEY。
agent = Agent(
    name="support",
    instructions="你是一名客服 agent。回答前先查询订单，然后用一两句具体的话回复。",
    model="glm-5.2",
    tools=[get_order],
)

# run_sync() 适合脚本和 notebook；在异步代码里，
# 请使用 `await Runner.run(agent, ...)`。
result = agent.run_sync("我的订单 A-1042 到哪了？")
print(result.output)
```

也可以一行命令启动完整聊天 UI：自带长期记忆、支持 skills 和定时任务，以及把当前目录作为工作区（提供文件读写和执行命令的能力）。

```bash
pip install "lovia[web]" && lovia web
```

Anthropic 也内置支持：配置 `ANTHROPIC_API_KEY`；使用非默认端点时再配置
`ANTHROPIC_BASE_URL`。模型名使用 `anthropic:` 前缀即可。模型相关的更多内容见
[Provider 与模型](https://cymoo.github.io/lovia/zh/providers/)。

## 文档

这份 README 只做快速介绍。更完整的内容见[文档](https://cymoo.github.io/lovia/zh/)；
建议先读[快速开始](https://cymoo.github.io/lovia/zh/quickstart/)和[核心概念](https://cymoo.github.io/lovia/zh/concepts/)。
[示例](./examples/README-zh.md)按编号组织，可以作为一条直接运行的学习路径。

## 为什么是 lovia

可组合的原语，普通的 Python，不另造一套抽象宇宙：

- **极简依赖。** 核心依赖只有 `httpx`、`pydantic` 和 `pyyaml`，其余按需安装即可。
- **抽象很少。** `Agent` 是不可变配置，`Runner` 执行一次运行，`@tool`
  就是带类型的函数；handoff 和 agent-as-tool 组合多个 agent；插件负责打包
  其余能力。
- **读得懂。** 关键流程集中，模型调用、工具执行、重试和持久化的顺序都很清楚。
  遇到意外行为时，可以顺着同一条链路查下去。
- **模型接入轻。** 内置 OpenAI、Anthropic 及其兼容接口。
  不堆厚重适配层；要接新模型，实现一个小 `Protocol` 就够了。
- **缓存友好的上下文管理。** 压缩只改变下一次模型调用能看到的内容，
  保持提示词前缀稳定，同时完整记录始终保留。
- **生产控制点清楚。** 审批、预算、取消、运行中追加指令、重试、快照/恢复，
  都是明确的开关，可以按你的应用需要接入。
- **扩展方式统一。** Skills、MCP、Todo、Memory 都是插件；要加入自定义的能力，
  也走同一套机制。

lovia 在设计上始终保持克制：能在应用层用少量代码组合出来的，
就不做成框架内置。

## 功能巡礼

下面每一站都附有完整指南。

### Agent

`Agent` 是声明式配置：不保存会话状态，可轻松 `clone()` 出变体。自定义工具、
工作区和插件等都可以直接组合到这份配置上：

```python
from lovia import Agent, Memory, Skills, tool
from lovia.workspace import Workspace


@tool
def light_travel_time(distance_km: float) -> str:
    """计算光走完一段距离需要多久。"""
    return f"{distance_km / 299_792.458:.2f} 秒"


agent = Agent(
    name="science-writer",
    instructions="你是一位科普作者，善于用生动的日常比喻讲清复杂的科学概念。",
    model="deepseek-v4-pro",
    tools=[light_travel_time],
    workspace=Workspace.local(".", mode="readonly"),
    plugins=[
        Skills("./skills"),
        Memory(),
    ],
)
```

→ [Agent](https://cymoo.github.io/lovia/zh/agents/)

### 运行与流式输出

一次运行，三种消费方式。流式句柄既可以异步迭代，也可以 await。迭代本身
不会抛出运行错误：每个流都会以且仅以一个终止事件结束。

```python
from lovia import Runner, events

handle = Runner.stream(agent, "火星距离地球 2.25 亿公里时，信号传过去需要多久？")

async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

→ [运行 agent](https://cymoo.github.io/lovia/zh/running/) · [流式输出](https://cymoo.github.io/lovia/zh/streaming/)

### 工具

带类型的 Python 函数就是工具。schema 来自函数签名、docstring、`Annotated`
和 Pydantic `Field`。同一轮里的多个工具调用默认并发执行；有不可重入副作用的
工具可以选择退出并发：

```python
from typing import Annotated
from pydantic import Field
from lovia import tool


@tool(strict=True)
def search_docs(
    query: Annotated[str, "搜索关键词"],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """搜索内部文档。"""
    return []


@tool(parallel=False)
async def apply_migration(name: str) -> str:
    """执行数据库迁移；不能和其他工具并发。"""
    return "applied"
```

→ [工具](https://cymoo.github.io/lovia/zh/tools/) · [内置工具](https://cymoo.github.io/lovia/zh/built-in-tools/)

### 结构化输出

传入 Pydantic 模型、dataclass、`TypedDict` 或普通类型；最终结果会被解析并
校验。解析失败时，默认会让模型修复一次：

```python
from pydantic import BaseModel
from lovia import Agent, Runner


class Brief(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(name="summarizer", model="glm-5.2", output_type=Brief)
result = await Runner.run(agent, "给 Python 开发者总结 Transformer。")
print(result.output.title)
```

→ [结构化输出](https://cymoo.github.io/lovia/zh/structured-output/)

### Provider

模型可以写字符串，也可以传 provider 实例。OpenAI 兼容端点读取
`OPENAI_BASE_URL` / `OPENAI_API_KEY`；Anthropic 默认走官方端点，读取
`ANTHROPIC_API_KEY`，非默认端点再设置 `ANTHROPIC_BASE_URL`。自定义 provider 只是一个小 `Protocol`：

```python
from lovia import Agent, ModelSettings

agent = Agent(
    name="assistant",
    model="anthropic:<model>",
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
```

→ [Provider 与模型](https://cymoo.github.io/lovia/zh/providers/)

### 多 Agent

两个原语，底层都是普通工具。**Handoff** 会移交对话：子 agent 带着完整历史接管并
直接回答用户。**Agent-as-tool** 则委派一个有边界的子任务：子 agent 只看到交给
它的提示词，结果作为工具结果返回。

```python
from lovia import Agent, Runner

billing = Agent(name="billing", instructions="处理账单问题。", model="glm-5.2")
support = Agent(name="support", instructions="处理技术问题。", model="glm-5.2")

triage = Agent(
    name="triage",
    instructions="把用户转给合适的专家。",
    model="deepseek-v4-flash",
    handoffs=[billing, support],       # handoff：专家接管对话
)
result = await Runner.run(triage, "我被重复扣款了。")
```

```python
summarizer = Agent(
    name="summarizer",
    instructions="用五个要点总结文本。",
    model="glm-5.2",
)

manager = Agent(
    name="manager",
    instructions="需要总结时，把任务委派给 summarizer。",
    model="deepseek-v4-flash",
    tools=[summarizer.as_tool(description="总结一段文本。")],  # 委派子任务
)
```

→ [多 Agent](https://cymoo.github.io/lovia/zh/multi-agent/)

### 人工介入

给敏感工具加门禁；决策可以来自 UI、服务端规则，也可以通过单独接口处理。无人决策时默认拒绝，
所以运行不会挂住：

```python
from lovia import Runner, events, tool


@tool(needs_approval=True)
async def refund(order_id: str, amount_cents: int) -> str:
    """发起退款。"""
    return "refunded"


async for ev in Runner.stream(agent, "给订单 A123 退款。"):
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()          # 或 ev.reject()
```

→ [工具审批](https://cymoo.github.io/lovia/zh/tools/#工具审批)

### Session 与 Checkpoint

Session 让对话跨运行持续；Checkpoint 让单次运行在崩溃后可恢复，并且具备幂等性。
重新提交一个已经完成的 `run_id` 会直接重放结果，不再调用模型：

```python
from lovia import CheckpointOptions, Runner, SQLiteCheckpointer, SQLiteSession

session = SQLiteSession("chat.db")
await Runner.run(agent, "我的项目叫 Atlas。", session=session, session_id="u1")

cp = SQLiteCheckpointer("runs.db")
result = await Runner.run(
    agent,
    "迁移报告格式。",
    checkpoint=CheckpointOptions(cp, "report-migration-42"),
)
```

→ [Session 与 Checkpoint](https://cymoo.github.io/lovia/zh/sessions-and-checkpoints/)

### 上下文压缩

长对话不会撑爆上下文窗口，而且不需要改写历史。压缩只改变发给 model 的视图，同时保持稳定的提示词前缀：

```python
from lovia import Agent, Compaction

agent = Agent(
    name="companion",
    model="glm-5.2",
    context_policy=Compaction(context_window=200_000, compact_at=0.85, compact_to=0.60),
)
```

→ [上下文管理](https://cymoo.github.io/lovia/zh/context/)

### 护栏与可靠性

护栏可以在输入/输出边界否决运行。重试这类默认行为配置在 agent 上；
每次请求的预算和时间限制，则在运行时传入：

```python
from lovia import Agent, RetryPolicy, RunBudget, Runner
from lovia.exceptions import GuardrailTripped


async def must_cite(output, ctx):
    if "source:" not in str(output).lower():
        return "缺少来源引用。"


agent = Agent(
    name="researcher",
    model="glm-5.2",
    output_guardrails=[must_cite],
    retry=RetryPolicy(max_attempts=2)
)

result = await Runner.run(
    agent,
    "分析这些日志。",
    budget=RunBudget(max_tool_calls=20, max_seconds=60)
)
```

→ [护栏](https://cymoo.github.io/lovia/zh/guardrails/) · [可靠性](https://cymoo.github.io/lovia/zh/reliability/)

### 观察与运行中调整

hooks 可以监听每个运行事件，事件类型和流式输出一致。通过 mailbox，
你可以在运行中追加一条用户消息，让模型在下一轮看到；运行本身也可以这样调整自己：

```python
from lovia import Mailbox, RunContext, Runner, events
from lovia.hooks import AgentHooks

hooks = AgentHooks()


@hooks.on(events.TurnStarted)
def deadline(ev, ctx: RunContext):
    if ev.turn == 9:
        ctx.mailbox.push("最后一轮：用已有信息回答。")


mailbox = Mailbox()
handle = Runner.stream(agent.clone(hooks=hooks), "分析这些日志。", mailbox=mailbox)
mailbox.push("重点看 14:00 左右的 5xx 峰值。")  # 下一轮可见
```

→ [可观测性](https://cymoo.github.io/lovia/zh/observability/) ·
[可靠性](https://cymoo.github.io/lovia/zh/reliability/#运行中追加指令)

### 工作区

文件和 shell 工具限定在一个根目录下，并由同一套 `allow`/`ask`/`deny` 策略
同时管理路径和命令。`ask` 决策走标准审批通道：

```python
from lovia import Agent
from lovia.workspace import CommandRule, Workspace

agent = Agent(
    name="coder",
    instructions="做小而明确的代码修改。",
    model="glm-5.2",
    workspace=Workspace.local(
        ".",
        mode="coding",
        denied_paths=(".env*",),
        command_rules=(CommandRule("pytest", "allow"), CommandRule("rm -rf", "deny")),
    ),
)
```

→ [工作区](https://cymoo.github.io/lovia/zh/workspace/)

### 插件

一条扩展轴：插件可以贡献工具、提示词、每轮视图注入器、hooks 和护栏，
但不接管控制流：

```python
from lovia import Agent, Skills, Todo
from lovia.plugins.mcp import MCP, MCPServerStdio

agent = Agent(
    name="builder",
    model="glm-5.2",
    plugins=[
        Todo(),
        Skills("./skills"),
        MCP(MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])),
    ],
)
```

- **`Todo()`**：给模型一份多步任务清单；当前清单每轮重新展示，但不会撑大
  transcript。
- **`Skills(dir)`**：可复用的指令包（`SKILL.md` + 附属文件）。提示词里常驻
  一行索引，完整内容按需加载。
- **`MCP(server)`**：接入 Model Context Protocol 服务器提供的工具，支持
  stdio 或 HTTP，可按服务器加名称前缀和审批门禁。

自己写插件，只需要一个 `name` 和一个返回贡献内容的异步 `setup()`。

→ [插件](https://cymoo.github.io/lovia/zh/plugins/) · [技能](https://cymoo.github.io/lovia/zh/skills/) ·
[MCP](https://cymoo.github.io/lovia/zh/mcp/)

### 记忆

跨对话的长期记忆：常驻提示词的 **Notes** 加可检索的 **Archive**。零配置可用，
也可以一次加一个参数逐步升级：

```python
from lovia import Agent, Memory
from lovia.plugins import OpenAIEmbedder

agent = Agent(name="assistant", model="glm-5.2",
              plugins=[Memory("./.lovia/memory")])

Memory("./memory")                             # 标准库关键词检索（FTS5 bm25）
Memory("./memory", embedder=OpenAIEmbedder())  # + 语义检索 -> 混合召回
Memory("./memory", index=None)                 # 只有 Notes，不建 Archive
```

→ [记忆](https://cymoo.github.io/lovia/zh/memory/)

### Web UI

一个轻量的 FastAPI 应用：SSE 流式输出、session、审批、定时任务、记忆编辑器等。
浏览器断开后，未完成的对话仍会在服务端继续：

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

```bash
lovia web --port 9000 --model glm-5.2      # python -m lovia.web 亦可
lovia web --model deepseek-v4-pro --base-url https://api.deepseek.com
```

缺失的必需配置（Base URL、API key、model name）首次运行时会交互式询问，并可保存到 `./.env`。配置优先级：命令行 flag > 环境变量 > `./.env`（或 `--env-file`）。

所有能力都以 JSON + SSE REST API 暴露（可在 `/api/docs`
浏览），因此你可以用 `create_app(agent, ui=False)`，或者把 router 挂到自己的
FastAPI 应用中，在同一套端点上做自定义前端。

→ [Web UI](https://cymoo.github.io/lovia/zh/web-ui/) · [Web 服务端](https://cymoo.github.io/lovia/zh/web-server/) · [HTTP API](https://cymoo.github.io/lovia/zh/http-api/)

### 评测

用 `Case` 定义输入和验收条件，跑完后生成报告。验收可以是普通函数，
也可以交给 LLM judge；报告还能在 CI 里和基线对比：

```python
from lovia.eval import Case, contains, evaluate, llm_judge, tool_called

report = await evaluate(agent, [
    Case("法国首都是哪里？", checks=[contains("巴黎")]),
    Case("23.4 * 91 等于多少？", checks=[tool_called("calculator")]),
    Case("为一家书店写一句雨天文案",
         checks=[llm_judge("不超过 30 个汉字，包含雨天和阅读意象，不使用感叹号")],
         samples=4, pass_threshold=0.75),
])
print(report)
assert report.passed
```

→ [评测](https://cymoo.github.io/lovia/zh/eval/)

### 测试

所有东西都可以离线跑在脚本化 provider 上：真实工具、真实循环、预置模型回复。

```python
from lovia.testing import ScriptedProvider, call, text

provider = ScriptedProvider([
    call("add", {"a": 2, "b": 3}, call_id="c1"),
    text("答案是 5。"),
])
```

→ [测试](https://cymoo.github.io/lovia/zh/testing/)

## 示例

`examples/` 目录是一条按编号排列、自包含、可直接运行的学习路径：从
`01_hello.py` 到终端客服 bot 共三十个脚本；另有每个内置工具族一个脚本
（`tools/`），以及经典 workflow 模式（`workflows/`）。
环境准备与完整索引见 [examples/README-zh.md](examples/README-zh.md)。

## 安装选项

| 需求 | 安装 |
| --- | --- |
| 核心框架 | `pip install lovia` |
| DuckDuckGo 搜索 | `pip install "lovia[ddg]"` |
| Tavily 搜索 | 无需安装额外依赖；设置 `TAVILY_API_KEY` 即可 |
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

标记为 `live_provider` 的真实端点测试默认跳过，需要显式开启。面向贡献者的
内部机制文档见 [docs/architecture.md](https://cymoo.github.io/lovia/zh/architecture/)。
