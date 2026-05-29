# lovia

一个轻量、provider 中立的 Python Agent 框架。

[English](./README.md)

```python
import asyncio
from lovia import Agent, Runner

agent = Agent(
    name="Assistant",
    instructions="你是一个乐于助人的助手。",
    model="openai:gpt-4o-mini",
)
result = asyncio.run(Runner.run(agent, "法国的首都是哪里？"))
print(result.output)  # 巴黎
```

**仅两个核心依赖**（`httpx`、`pydantic`）。无 DSL，无图，无全局状态。
工具、会话、handoff、结构化输出、MCP、流式输出——全部按需启用。

---

## 安装

```bash
pip install lovia
```

可选扩展：

```bash
pip install "lovia[mcp]"    # Model Context Protocol 客户端
pip install "lovia[tools]"  # web_search（DuckDuckGo 后端）
pip install "lovia[web]"    # FastAPI + SSE 聊天服务器
```

---

## 工具

用 `@tool` 把任意带类型注解的函数变成工具，同步和异步均可。

```python
from lovia import Agent, Runner, tool

@tool
def calculate(expression: str) -> float:
    """计算简单的数学表达式。"""
    return eval(expression, {"__builtins__": {}})

agent = Agent(
    name="Calc",
    instructions="需要计算时调用 calculate()。",
    model="openai:gpt-4o-mini",
    tools=[calculate],
)
result = asyncio.run(Runner.run(agent, "1337 * 42 等于多少？"))
```

用 `Annotated` 为每个参数添加描述：

```python
from typing import Annotated

@tool
def search(
    query: Annotated[str, "搜索关键词。"],
    limit: Annotated[int, "最多返回多少条，1-20。"] = 5,
) -> list[str]: ...
```

简单执行策略仍然用装饰器参数：

```python
@tool(timeout=5, retries=2, needs_approval=True)
async def send_email(to: str, body: str) -> str: ...
```

高级场景可以传入可组合的 `policies`，同时保留简单参数：

```python
from lovia import RunContext

async def redact(next_tool, args, ctx):
    result = await next_tool(args, ctx)
    return str(result).replace(ctx.context.api_key, "[redacted]")

@tool(policies=[redact])
async def call_api(ctx: RunContext, path: str) -> str: ...
```

---

## 结构化输出

将任意 Pydantic 模型作为 `output_type` 传入，结果会自动校验。
`output_repair=True` 让模型在首次解析失败时自我修正。

```python
from pydantic import BaseModel
from lovia import Agent, Runner

class Review(BaseModel):
    rating: int       # 1-5
    summary: str
    pros: list[str]
    cons: list[str]

agent = Agent(
    name="Reviewer",
    instructions="从用户文本中提取结构化评测。",
    model="openai:gpt-4o-mini",
    output_type=Review,
    output_repair=True,
)
result = asyncio.run(Runner.run(agent, "电池续航很棒，但屏幕偏暗。"))
print(result.output.rating)   # -> int
```

单次调用覆盖输出类型，不影响 Agent 定义：

```python
result = await Runner.run(agent, "用纯文本总结一下。", output_type=str)
```

---

## 流式输出

```python
async for event in Runner.stream(agent, "讲个笑话"):
    print(event)
```

或直接从 agent 实例调用：

```python
async for event in agent.stream("讲个笑话"):
    print(event)
```

---

## 动态 instructions

用 `@agent.system_prompt` 在运行时注入上下文内容，多个片段与基础 `instructions` 自动拼接。

```python
agent = Agent(name="Support", instructions="你是客服机器人。", model="openai:gpt-4o-mini")

@agent.system_prompt
async def inject_user(ctx) -> str:
    user = await db.get_user(ctx.context.user_id)
    return f"用户名：{user.name}，套餐：{user.plan}。"

# 单次追加临时上下文：
result = await Runner.run(agent, "我需要帮助。", append_instructions="请用英文回复。")
```

如果要构造可复用 Agent，优先使用函数式配置：

```python
agent = agent.with_system_prompt(inject_user)
```

---

## Handoff（Agent 转交）

Agent 可以在对话中途将控制权交给另一个 Agent，Runner 自动跟随转交链。

```python
billing = Agent(name="Billing", instructions="处理账单问题。", model="openai:gpt-4o-mini")
support = Agent(
    name="Support",
    instructions="回答支持问题。账单问题转交给 Billing。",
    model="openai:gpt-4o-mini",
    handoffs=[billing],
)
result = await Runner.run(support, "我可以申请退款吗？")
```

---

## 会话（持久化对话历史）

通过 `session=` 参数跨调用保留对话历史，默认提供内存存储，可换为 Redis 或数据库。

```python
from lovia.stores import InMemorySessionStore

session_store = InMemorySessionStore()

result1 = await Runner.run(agent, "我叫 Alice。", session=session_store.session("u42"))
result2 = await Runner.run(agent, "我叫什么？", session=session_store.session("u42"))
# → "你叫 Alice。"
```

---

## 人工审批

用 `needs_approval=True` 标记敏感工具，需要人工确认后才能执行。

```python
from lovia import ApprovalChannel

channel = ApprovalChannel()

@tool(needs_approval=True)
def send_email(to: str, body: str) -> str: ...

# 在 UI 中调用 channel.approve(request_id) 或 channel.deny(request_id, reason)
result = await Runner.run(agent, "给 alice@example.com 发一封欢迎邮件", approval_channel=channel)
```

---

## 同步入口

`Runner.run_sync` 和 `agent.run_sync` 封装了 `asyncio.run`，适合在脚本或无法 `await` 的场景使用。

```python
result = Runner.run_sync(agent, "2+2 等于多少？")
print(result.output)
```

---

## Tools（工具）

`lovia.tools` 提供开箱即用的实用工具，无需任何配置即可使用。
**没有任何工具会被自动导入**，按需取用。

```python
from lovia.tools.http import http_fetch
from lovia.tools.search import duckduckgo_search_tool
from lovia.tools.todo import TodoList, todo_tools
from lovia.tools.human import HumanChannel, ask_human
from lovia.tools.think import think
from lovia.tools.time import now

todos = TodoList()
channel = HumanChannel()

agent = Agent(
    name="Worker",
    instructions="规划、推理、执行。",
    model="openai:gpt-4o-mini",
    tools=[
        http_fetch, now, think,
        duckduckgo_search_tool(),  # 需要 lovia[tools]
        *todo_tools(todos),
        ask_human(channel),
    ],
)
```

工具约定：无状态工具导出可直接使用的 `Tool`；可插拔后端使用 factory；有状态单工具对象提供 `.tool()`；有状态多工具对象提供 `.tools()`。

文件系统、Shell 等工具也在同一个 `lovia.tools` 命名空间里；可以由 `Agent(sandbox=...)` 自动注入，也可以用 `coding_tools(root=".")` 直接创建。

每个工具的可运行示例见 [`examples/tools/`](./examples/tools/)。

---

## Sandbox（沙箱）

`lovia.sandbox` 是简洁的文件系统 + 进程层。给 Agent 一个 sandbox，Lovia
会自动注入常用的文件和 Shell 工具；如果你已经在 Docker 或其他受控环境中
运行，也可以直接使用同一套 `lovia.tools` 工具。

```python
from lovia import Agent, Runner
from lovia.sandbox import Sandbox

agent = Agent(
    name="coder",
    instructions="You are a focused coding agent.",
    model="openai:gpt-4o-mini",
    sandbox=Sandbox.local("."),
)

await Runner.run(agent, "创建 app.py 并运行。")
```

开箱即得：

* **路径越权保护**——符号链接感知，文件工具只接受 root 内相对路径。
* **简单原子工具**——`read_file`、`write_file`、`edit_file`、`glob`、
  `list_dir`、`shell` 实现一次，既可由 `Agent(sandbox=...)` 自动注入，
  也可通过 `lovia.tools.coding_tools(root=".")` 直接使用。
* **精确编辑**——`edit_file` 用 `old`/`new` 替换唯一匹配；缺失或多重匹配
  会返回可恢复失败，方便模型重读后重试。
* **结构化命令结果**——`shell` 返回 `exit_code`、`stdout`、`stderr`、
  `timed_out`、`truncated`。
* **审批感知 Shell**——默认 `mode="coding"` 允许文件读写，但 Shell 命令走
  Lovia 现有审批流；自动化场景可显式使用 `mode="trusted"`。
* **隐藏文件过滤**——`list_dir`/`glob` 默认跳过点开头条目。

`Sandbox.local(".")` **不是**安全边界。它限制 Lovia 文件工具的根目录，并按
策略控制 Shell；但批准后的命令仍以宿主用户执行，写入也会修改真实文件。
需要强隔离时，后续可通过实现 `SandboxBackend` 接入 Docker / remote 后端。

可运行示例：[`examples/22_sandbox.py`](./examples/22_sandbox.py)、
[`examples/23_sandbox_agent.py`](./examples/23_sandbox_agent.py)。

---

## Skills（技能包）

Skills 是以 Markdown 驱动的知识模块，存放在目录树中，让你无需膨胀 system prompt 就能组合领域知识。

```
skills/
  translation/
    SKILL.md          # 名称、描述、使用说明
    references/       # Agent 可读取的参考文件
```

```python
from lovia.skills import SkillCatalog

catalog = SkillCatalog.from_dir("./skills")  # 默认惰性加载
agent = Agent(
    name="Expert",
    instructions=catalog.render_catalog(),
    model="openai:gpt-4o-mini",
    tools=catalog.tools(),
)
```

惰性模式下 catalog 渲染为简洁索引，模型按需调用 `load_skill` 加载完整内容；
`mode="eager"` 则将所有技能内容直接内联进 system prompt。

---

## 多 Provider

`model=` 接受 `"provider:model"` 字符串或任意 `Provider` 实例。

```python
# OpenAI
agent = Agent(model="openai:gpt-4o-mini", ...)
# Anthropic
agent = Agent(model="anthropic:claude-3-5-haiku-20241022", ...)
# 任意 OpenAI 兼容接口（DeepSeek、Ollama、vLLM 等）
from lovia import OpenAIChatProvider
provider = OpenAIChatProvider(model="deepseek-chat", base_url="https://api.deepseek.com/v1", api_key="...")
agent = Agent(model=provider, ...)
```

---

## 示例

```
examples/
  01_hello.py                  最小 Agent
  02_tools.py                  工具调用
  03_streaming.py              流式输出
  04_structured_output.py      Pydantic 结构化输出
  05_handoff.py                Agent 间转交
  06_agent_as_tool.py          子 Agent 作为工具
  07_session.py                持久化会话
  08_skills.py                 SkillCatalog
  09_compat_provider.py        自定义 OpenAI 兼容 Provider
  10_hooks.py                  生命周期 hooks / tracing
  11_approval.py               人工审批
  12_multimodal.py             图片输入
  13_budget_and_cancel.py      Token 预算与取消
  14_guardrails.py             输入/输出守卫
  15_resume.py                 恢复中断的运行
  16_web_serve.py              FastAPI + SSE 服务器
  17_responses_reasoning.py    OpenAI Responses API + 推理模型
  18_context_policy.py         长对话自动摘要
  19_dynamic_instructions.py   动态 system prompt
  20_builtins.py               多个内置工具组合
  21_dx.py                     Annotated 参数、run_sync
  tools/                       每个工具一个专项 demo
  workflows/                   多 Agent 工作流模式
```

---

## 开发

```bash
git clone https://github.com/cymoo/lovia
pip install -e ".[dev]"
pytest          # 测试
ruff check .    # lint
mypy lovia      # 类型检查
```

架构说明、设计哲学与提交约定见 [`AGENTS.md`](./AGENTS.md)。

---

MIT License
