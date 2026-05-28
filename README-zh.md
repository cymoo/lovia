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

## 内置工具

`lovia.builtins` 提供开箱即用的实用工具，无需任何配置即可使用。
**没有任何工具会被自动导入**，按需取用。

```python
from lovia.builtins.http import http_fetch
from lovia.builtins.search import duckduckgo_search_tool
from lovia.builtins.todo import TodoList, todo_tools
from lovia.builtins.human import HumanChannel, ask_human
from lovia.builtins.think import think
from lovia.builtins.time import now

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

内置工具约定：无状态工具导出可直接使用的 `Tool`；可插拔后端使用 factory；有状态单工具对象提供 `.tool()`；有状态多工具对象提供 `.tools()`。

文件系统、Shell 等"重型"工具不在 builtins 里——它们由下一节的 `lovia.sandbox` 统一提供，自带路径越权保护与审计策略。

每个工具的可运行示例见 [`examples/builtins/`](./examples/builtins/)。

---

## Sandbox（沙箱）

`lovia.sandbox` 是简洁的文件系统 + 进程层。一个 Protocol（`Sandbox`）、
一个池（`SandboxProvider`）、一行接线（`attach_sandbox`）即可。默认随包
携带 `LocalSandbox`（进程内）；要切换 Docker / Firecracker，只需实现
Protocol——Agent 代码完全不动。

```python
from lovia import (
    Agent, Runner,
    LocalSandboxProvider, attach_sandbox, AuditStream,
)
from lovia.stores import InMemorySession

base = Agent(name="coder", instructions="…", model="openai:gpt-4o-mini")

async with LocalSandboxProvider() as provider:
    audit = AuditStream()  # 可选：给 UI 订阅
    agent = attach_sandbox(base, provider, audit_stream=audit)

    session = InMemorySession()  # from lovia.stores
    await Runner.run(agent, "创建 app.py 并运行。", session=session, session_id="s1")
    # 相同 session_id → 下一轮复用同一工作区。
    await Runner.run(agent, "现在补一组测试。", session=session, session_id="s1")
```

开箱即得：

* **路径越权保护**——符号链接感知，拦截 `..`、`/etc/...` 等。
* **基于 PATH/HOME 的依赖隔离**——每个沙箱把 `HOME` 和 `TMPDIR` 重定向到
  私有子目录，并将 `<root>/.venv/bin` 前置到 `PATH`。框架**不**管理这个
  venv：当大模型需要 Python 依赖时，它自己启动一个：
  ```bash
  python -m venv .venv && .venv/bin/pip install pandas
  ```
  此后的命令会自动把 `python` / `pip` 解析到这个 venv。无需任何特殊 API、
  无自动 bootstrap、不污染宿主环境。
* **审计策略**——`default_audit_policy()` 拦截显而易见的危险命令
  （`rm -rf /`、`mkfs`、`curl|sh`、fork bomb 等），并对裸的
  `pip install` / `npm install -g` 发出 `warn`，引导大模型走 venv。
  三态判定（`pass`/`warn`/`block`）：警告会注入 stderr 而不阻断，
  让模型有机会在下一轮自我修正。
* **按 session 生命周期**——沙箱按 `session_id` 引用计数；多轮对话
  天然复用同一工作区（包括模型自己建的 `.venv`），直到 provider 关闭。
* **隐藏文件过滤**——`ls`/`glob` 默认跳过点开头条目，`**/*.py` 不会被
  模型自己的 `.venv/` 淹没。需要时传 `include_hidden=True`。
* **实时审计流**——`AuditStream.subscribe()` 给 UI 订阅，历史会保留以
  服务后加入者。
* **apply_patch 工具**——容错的 unified-diff 编辑器，给模型改文件的
  最小代价方案。

`LocalSandbox` **不是**安全边界——`HOME`/`PATH` 重定向只是保持整洁，不保证
安全。执行不可信代码请用基于容器的实现。

可运行示例：[`examples/22_sandbox.py`](./examples/22_sandbox.py)、
[`examples/23_sandbox_session.py`](./examples/23_sandbox_session.py)、
[`examples/24_custom_sandbox.py`](./examples/24_custom_sandbox.py)。

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
  builtins/                    每个内置工具一个专项 demo
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
