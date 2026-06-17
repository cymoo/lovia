# lovia

[English README](./README.md)

lovia 是一个轻量的 Python Agent 框架，适合那些想要“一个清楚可靠的 agent loop”，而不是一整套平台的人。它提供工具调用、流式输出、结构化输出、会话、handoff、护栏、审批、workspace、skills、plugins、MCP 和一个小型 Web UI，同时保持核心代码可读、可替换、可扩展。

```bash
pip install lovia
```

```python
import asyncio
from lovia import Agent, Runner, tool


@tool
def add(a: int, b: int) -> int:
    """把两个整数相加。"""
    return a + b


async def main() -> None:
    agent = Agent(
        name="calculator",
        instructions="需要时使用工具，回答要简短。",
        model="deepseek-v4-pro",
        tools=[add],
    )
    result = await Runner.run(agent, "21 + 21 等于多少？")
    print(result.output)


asyncio.run(main())
```

使用 OpenAI 官方接口时设置 `OPENAI_API_KEY`；如果你用的是 OpenAI 兼容接口，设置 `OPENAI_BASE_URL` 即可。Anthropic 也内置支持：`model="anthropic:claude-4-5-sonnet"`。

## 为什么是 lovia

很多 Agent 框架很快会变成“另一个平台”。lovia 的取舍不一样：框架应该小到你能理解它，也应该认真到可以拿去做产品。

- **心智模型很小。** `Agent` 描述行为，`Runner` 负责执行，`@tool` 暴露 Python 函数。大多数能力都从这三个概念自然展开。
- **模型中立。** 内置 OpenAI Chat Completions 和 Anthropic Messages 适配器，直接用 `httpx` 请求；自定义 provider 只需要实现一个小 `Protocol`。
- **Python 原生扩展。** Agent 是 dataclass，provider、session、plugin、skill、workspace 都是协议形状。你是在接入自己的代码，不是在继承一座框架大厦。
- **默认轻量。** 核心安装保持克制。搜索、MCP、Web UI、示例脚本的美化依赖、编排集成都放在 extras 里。
- **生产原语齐全。** 审批、护栏、重试、预算、取消、checkpoint/resume、上下文压缩、生命周期 hooks、带策略的 workspace 工具，需要时都能拿出来用。

## 设计哲学

lovia 的优先级：

1. **Concise 简洁。** 一个功能应该能装进脑子里。公共 API 要直观，必要时也能读懂内部实现。
2. **Lightweight 轻量。** 核心应该安装干净、导入迅速，不把你没要的基础设施带进来。
3. **Extensible 易扩展。** 真实应用一定会有自己的 provider、存储、策略、工具和 UI。lovia 提供扩展点，而不是锁死路径。
4. **General-purpose 通用。** 内置能力是实用工具，也是扩展点的示范；你可以用同样的接口替换它们。

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

动态系统提示可以读取每次运行传入的 context：

```python
@agent.system_prompt
async def user_tier(ctx) -> str:
    return f"用户等级：{ctx.context['tier']}"
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

`stream()` 返回的 handle 既可以异步迭代，也可以 await：

```python
from lovia import events

handle = Runner.stream(agent, "用一段话解释 context window。")

async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

脚本场景可以用同步包装：

```python
result = Runner.run_sync(agent, "总结这个文件。")
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
        "anthropic:claude-4-5-sonnet",
        "deepseek-v4-pro",
    ],
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
```

自定义 provider 实现 `Provider` 协议即可，也可以通过 `lovia.providers` entry point 注册。

## 多 Agent 工作流

### Handoff

handoff 让一个 agent 把控制权交给专家 agent。transcript 会跟随移交，也可以通过 filter 清理。

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

子 agent 会在独立 loop 中运行，最终输出作为工具结果返回。

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
agent = agent.clone(
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

跨 session 的长期记忆不是核心概念——没有 `Memory` 类型。把它做成一个 plugin、包裹你自己的存储即可（见下方 Plugins 里的 `MemoryPlugin` 示例）。

## 上下文管理

长对话默认使用 `Compaction`。它只改变“本次发给模型的视图”：完整 transcript 仍保存在 session/checkpoint 中；在 token 压力下，模型调用视图可以归档超大工具结果、清理较旧工具结果，必要时总结旧历史。

```python
from lovia import Compaction, Runner

policy = Compaction(
    context_window=200_000,
    compact_at=0.75,
    compact_to=0.50,
)

result = await Runner.run(agent, "继续。", context_policy=policy)
```

如果希望模型在压缩后无需重跑工具也能找回某个工具结果，可以加入 `recall_tool_result`：

```python
from lovia.tools import recall_tool_result

agent = agent.clone(tools=[*agent.tools, recall_tool_result])
```

传入 `NoopContextPolicy()` 可以关闭自动压缩。

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

生命周期 hooks 接收的就是流式输出同一套类型化事件：

```python
from lovia import events
from lovia.hooks import AgentHooks

hooks = AgentHooks()


@hooks.on(events.ToolCallStarted)
async def log_tool(ev):
    print(ev.call.name, ev.call.arguments)


agent = agent.clone(hooks=hooks)
```

## 内置工具

工具不会自动塞进 agent。你按需选择。

```python
from lovia.tools.http import http_fetch
from lovia.tools.time import now
from lovia.tools.search import duckduckgo_search_tool

agent = Agent(
    name="researcher",
    model="deepseek-v4-pro",
    tools=[http_fetch, now, duckduckgo_search_tool()],
)
```

DuckDuckGo 搜索支持需要安装：

```bash
pip install "lovia[ddg]"
```

如果你有自己的搜索后端，实现 `WebSearch` 并传给 `web_search()` 即可。

## Plugins

**Plugin** 是 lovia 唯一的扩展轴，用来把一个功能打包成一个对象。单个 plugin 可以贡献任意组合：`tools`、系统提示 `instructions`、每轮注入的 `view_injectors`（临时提醒，永不写入 transcript）、事件 `hooks`，以及 `input_guardrails` / `output_guardrails`。runner 在**每次 run**（以及 handoff 时每个 agent）通过 await 其异步 `setup()` 来激活每个 plugin，并在 run 结束时通过 `aclose()` 释放它打开的资源。Plugin 是纯增量的——它们不驱动控制流；中止、重试、handoff 始终由 loop 掌控。下面的 Skills、MCP 和 todo 列表都是内置 plugin。

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

### 编写 plugin

一个 plugin 就是任意带有 `name` 和返回 `PluginInstance` 的 `async setup()` 的对象。需要**每次 run 全新**的状态放在 `setup` 内部（如上面的 todo 列表）；需要**跨 run、跨 session 持久化**的状态则挂在 plugin 上、在构造时传入。下面是一个长期记忆 plugin——它包裹一个你自己实现、只创建一次、被每次 run 共享的后端，于是 agent 能在下一次对话里回忆起上一次的事实：

```python
from dataclasses import dataclass
from typing import Protocol

from lovia import Agent, PluginInstance, tool


class MemoryStore(Protocol):
    """你的长期后端——用向量库、SQLite 等实现它。"""

    async def add(self, fact: str) -> None: ...
    async def search(self, query: str, k: int) -> list[str]: ...


@dataclass
class MemoryPlugin:
    """跨 session 的长期记忆，agent 可以写入并检索。"""

    store: MemoryStore  # 长生命周期，被每次 run 共享——不在每次 run 重建
    name: str = "memory"

    async def setup(self) -> PluginInstance:
        store = self.store

        @tool
        async def remember(fact: str) -> str:
            """保存一条持久事实，供以后任意 session 回忆。"""
            await store.add(fact)
            return "已存入长期记忆。"

        @tool
        async def recall(query: str) -> str:
            """在长期记忆中检索与 query 相关的事实。"""
            hits = await store.search(query, k=5)
            return "\n".join(f"- {h}" for h in hits) or "（没有相关内容）"

        return PluginInstance(
            tools=[remember, recall],
            instructions=(
                "你拥有跨 session 持久的长期记忆。回答前先用 `recall` 查一查，"
                "并用 `remember` 存下用户透露的持久事实或偏好。"
            ),
        )


store = MyVectorStore()  # 你的 MemoryStore：只需两个异步方法 add() 和 search()
agent = Agent(name="assistant", model="deepseek-v4-pro", plugins=[MemoryPlugin(store)])
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

Workspace 路径都是 root-relative；绝对路径、`..` 逃逸和符号链接逃逸都会被拒绝。本地 shell 仍以宿主机用户身份运行；如果你需要强隔离，请使用容器或远程 workspace backend。

## Web UI

可选 Web 层是一个小型 FastAPI 应用，包含 SSE 流式输出、sessions、Markdown 渲染和审批路由。

```bash
pip install "lovia[web]"
```

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

## 安装 Extras

| 需求 | 安装 |
| --- | --- |
| 核心框架 | `pip install lovia` |
| DuckDuckGo 搜索 | `pip install "lovia[ddg]"` |
| MCP 集成 | `pip install "lovia[mcp]"` |
| Web UI | `pip install "lovia[web]"` |
| 运行示例 | `pip install "lovia[examples,web]"` |
| 开发、测试、发布 | `pip install -e ".[dev]"` |

`examples` 是运行演示脚本所需的依赖，例如 `python-dotenv`、`rich`、`prefect` 和 `ddgs`。`dev` 是维护这个仓库所需的依赖，例如 `pytest`、`ruff`、`mypy`、`build`、`twine` 以及 Web 测试依赖。二者故意分开，避免普通开发安装演示专用依赖。

## 开发

```bash
pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format .
.venv/bin/python -m mypy lovia
```

`examples/` 目录里有主要能力的可运行脚本。真实 provider 端到端测试带有 `live_provider` 标记，默认不会运行。
