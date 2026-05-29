# lovia

lovia 是一个轻量、异步优先、provider-neutral 的 Python Agent 框架。核心保持很小，
只提供 Agent 循环、工具、结构化输出、会话等基础能力；搜索、MCP、Web UI、Rich
终端输出、Prefect 工作流都放在可选依赖里。

```bash
pip install lovia
```

```python
import asyncio
from lovia import Agent, Runner, tool


@tool
async def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


async def main() -> None:
    agent = Agent(
        name="calc",
        instructions="回答要简短；需要时使用工具。",
        model="openai:gpt-4o-mini",
        tools=[add],
    )
    result = await Runner.run(agent, "2 + 3 等于多少？")
    print(result.output)


asyncio.run(main())
```

## 为什么选择 lovia？

- **核心轻**：硬依赖只有 `httpx` 和 `pydantic`。
- **异步优先**：公开 API 都是 async；同步方法只是便捷封装。
- **模型中立**：可以使用 OpenAI-compatible chat、OpenAI Responses，或自定义 Provider。
- **工具简单**：一个带类型注解的 Python 函数，加上 `@tool` 就能给模型调用。
- **适合 coding agent**：`Agent(sandbox=Sandbox.local("."))` 自动接入文件和 shell 工具。
- **可选层清晰**：Web、MCP、搜索、Rich 示例、Prefect 示例都在 extras 中。

## 安装 extras

| 需求 | 安装 |
| --- | --- |
| 核心框架 | `pip install lovia` |
| 示例中加载 `.env` | `pip install "lovia[dotenv]"` |
| DuckDuckGo 搜索工具 | `pip install "lovia[tools]"` |
| MCP 集成 | `pip install "lovia[mcp]"` |
| Web UI | `pip install "lovia[web]"` |
| Rich 终端示例 | `pip install "lovia[rich]"` |
| Prefect 工作流示例 | `pip install "lovia[prefect]"` |
| 运行所有示例常用依赖 | `pip install "lovia[examples,web]"` |
| 开发 | `pip install -e ".[dev]"` |

## 核心概念

### Agent

`Agent` 是一个 dataclass，用来描述这个助手：名字、指令、模型、工具、输出类型、
hooks、sandbox 和运行策略。

```python
agent = Agent(
    name="writer",
    instructions="回答要具体、简洁。",
    model="openai:gpt-4o-mini",
)
```

### Runner

`Runner` 负责执行 Agent 循环：把消息发给模型、执行模型请求的工具、追加工具结果，
直到模型返回最终答案。

```python
result = await Runner.run(agent, "写一段 release note。")
print(result.output)
```

流式输出会返回类型化事件：

```python
from lovia import events

handle = Runner.stream(agent, "讲一个很短的故事。")
async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

### Tool

带类型注解的 Python 函数可以直接变成工具。lovia 会根据类型、docstring 和
`Annotated`/`Field` 生成 JSON Schema。

```python
from typing import Annotated
from pydantic import Field
from lovia import tool


@tool(strict=True)
async def search_docs(
    query: Annotated[str, Field(description="搜索关键词")],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """搜索内部文档。"""
    return [f"result for {query}"]
```

敏感工具可以设置 `needs_approval=True`。流式运行时，Runner 会发出
`ApprovalRequired` 事件，由 CLI 或 Web UI 决定批准或拒绝。

## 内置工具

实用工具统一放在 `lovia.tools` 下。它们不会自动导入，按需使用即可。

```python
from lovia.tools.http import http_fetch
from lovia.tools.search import duckduckgo_search_tool
from lovia.tools.todo import TodoList, todo_tools
from lovia.tools.human import HumanChannel, ask_human
from lovia.tools.time import now
from lovia.tools.think import think

todos = TodoList()
agent = Agent(
    name="assistant",
    model="openai:gpt-4o-mini",
    tools=[
        http_fetch,
        duckduckgo_search_tool(),
        *todo_tools(todos),
        now,
        think,
    ],
)
```

专项示例见 [`examples/tools/`](./examples/tools/)。

## Sandbox 与 coding tools

coding agent 不需要手动拼接每个文件工具，直接给 Agent 绑定 sandbox：

```python
from lovia import Agent
from lovia.sandbox import Sandbox

agent = Agent(
    name="coder",
    instructions="做小而安全的代码修改。",
    model="openai:gpt-4o-mini",
    sandbox=Sandbox.local(".", mode="coding"),
)
```

`mode="coding"` 会暴露 read/write/edit/list/glob 和需要审批的 shell；
`mode="readonly"` 只暴露 read/list/glob；`mode="trusted"` 允许 shell 不经审批执行。

也可以直接使用工具 factory：

```python
from lovia.tools import coding_tools

agent = Agent(
    name="coder",
    model="openai:gpt-4o-mini",
    tools=coding_tools(root=".", mode="coding"),
)
```

本地 sandbox 只接受相对路径，会拒绝绝对路径、`..` 逃逸和 symlink 逃逸。注意：
本地 shell 仍然以当前系统用户执行；它是一个好用的边界，不是强安全沙箱。

## 结构化输出

传入 Pydantic 模型即可得到校验后的结果：

```python
from pydantic import BaseModel


class Summary(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(
    name="summarizer",
    model="openai:gpt-4o-mini",
    output_type=Summary,
)
result = await Runner.run(agent, "总结 lovia。")
print(result.output.title)
```

每次调用也可以临时覆盖输出类型：

```python
result = await Runner.run(agent, "返回 JSON 摘要。", output_type=Summary)
```

## 会话与长对话

会话可以保存多轮上下文：

```python
from lovia.stores import SQLiteSession

session = SQLiteSession("chat.db")
await Runner.run(agent, "记住我的项目叫 Atlas。", session=session, session_id="u1")
await Runner.run(agent, "我的项目叫什么？", session=session, session_id="u1")
```

长对话可以加 context policy，接近模型窗口时自动压缩旧消息：

```python
from lovia import SummarizingContextPolicy

policy = SummarizingContextPolicy(keep_recent_messages=10)
result = await Runner.run(agent, "继续", context_policy=policy)
```

## Web UI

可选 Web 层提供一个克制的 FastAPI 应用：

- SSE 流式输出；
- 传入 `db_path` 后持久化聊天会话；
- 敏感工具的 HTTP 审批；
- assistant 消息的安全 Markdown 渲染；
- Jinja2 模板渲染的零构建聊天页面。

```bash
pip install "lovia[web,dotenv]"
python examples/16_web_serve.py
```

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

## Prefect 工作流

lovia 可以自然嵌入工作流系统。Prefect 示例把一次 Agent 调用包装成可重试 task，
再由 flow 编排：

```bash
pip install "lovia[examples]"
python examples/24_prefect.py
```

```python
from prefect import flow, task
from lovia import Agent, Runner


@task(retries=1)
async def ask_agent(topic: str) -> str:
    result = await Runner.run(Agent(name="planner", model="openai:gpt-4o-mini"), topic)
    return str(result.output)


@flow
async def plan() -> str:
    return await ask_agent("规划一次小版本发布")
```

## 示例索引

| 文件 | 内容 |
| --- | --- |
| `examples/01_hello.py` | 最小 Agent |
| `examples/02_tools.py` | 自定义 `@tool` |
| `examples/03_streaming.py` | Rich 流式输出 |
| `examples/04_structured_output.py` | Pydantic 结构化输出 |
| `examples/05_handoff.py` | Agent handoff |
| `examples/08_skills.py` | Skill catalog |
| `examples/11_approval.py` | 工具审批 |
| `examples/16_web_serve.py` | Web UI |
| `examples/22_sandbox.py` | 直接使用 sandbox session |
| `examples/23_sandbox_agent.py` | 带 sandbox 的 coding agent |
| `examples/24_prefect.py` | Prefect flow 集成 |
| `examples/tools/` | 各工具专项示例 |
| `examples/workflows/` | 常见工作流模式 |

## 开发

```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy lovia
pytest -q
```

设计原则很简单：核心保持小；新集成优先放到可选 extras、示例或用户侧 recipe，除非它
能显著简化框架本身。
