# lovia

一个轻量、provider 中立的 Python agent 框架。

[English](./README.md)

```python
from lovia import Agent, Runner

agent = Agent(name="Greeter", instructions="用一句话回复。", model="openai:gpt-4o-mini")
print((await Runner.run(agent, "用三种语言打招呼。")).output)
```

- **不引入 DSL、图、全局状态** —— 纯 Python，类型清晰。
- **核心仅两个依赖** —— `httpx` 与 `pydantic`，其它能力按需启用。
- **Async 优先**，必要处提供 `run_sync` 同步入口。
- **Provider 中立** —— OpenAI Chat & Responses、Anthropic，以及任何 OpenAI 兼容服务。

---

## 安装

```bash
pip install lovia                 # 核心
pip install "lovia[mcp]"          # + MCP 客户端
pip install "lovia[tools]"        # + web_search 的 DuckDuckGo 后端
pip install "lovia[web]"          # + FastAPI / SSE + 自带聊天 UI
pip install "lovia[dev]"          # + pytest, ruff, mypy
```

Python 3.10+。

---

## 快速上手

```python
import asyncio
from lovia import Agent, Runner, tool

@tool
def add(a: int, b: int) -> int:
    """两数求和。"""
    return a + b

agent = Agent(
    name="Calc",
    instructions="需要计算时调用工具。",
    model="openai:gpt-4o-mini",
    tools=[add],
)

print(asyncio.run(Runner.run(agent, "17 + 25 等于多少？")).output)
```

同步入口（脚本 / notebook 友好）：

```python
result = Runner.run_sync(agent, "17 + 25?")
# 或直接：
result = agent.run_sync("17 + 25?")
```

流式：

```python
async for event in agent.stream("讲个笑话"):
    print(event)
```

---

## 核心概念

### Agent

```python
agent = Agent(
    name="Concierge",
    instructions="简短回答。",        # str 或 (ctx) -> str | Awaitable[str]
    model="openai:gpt-4o-mini",     # "provider:model" 或 Provider 实例
    tools=[...],
    output_type=MyPydanticModel,    # 任何 Pydantic 可校验的类型
    handoffs=[other_agent],         # 转交给其它 agent
)
```

### 动态 instructions

用 `@agent.system_prompt` 在配置期追加片段，或在调用期通过
`append_instructions=` 临时追加。两者都与静态 `instructions=` 自动拼接。

```python
agent = Agent(name="Helper", instructions="你是一个乐于助人的助手。")

@agent.system_prompt
def add_user(ctx) -> str:
    user = getattr(ctx.context, "user", None)
    return f"用户名是 {user}。" if user else ""

await Runner.run(agent, "你好", append_instructions="请用俳句回复。")
```

### 单次覆盖 `output_type`

Agent 上声明的 `output_type` 是默认值；`Runner.run`（以及 `agent.run`）
可对单次调用覆盖之。传 `None` 表示退回纯文本。

```python
class Plan(BaseModel):
    steps: list[str]

agent = Agent(name="x", instructions="...", output_type=Plan)
plan = (await Runner.run(agent, "规划旅程")).output           # -> Plan
text = (await Runner.run(agent, "规划旅程", output_type=None)).output  # -> str
```

### 工具

`@tool` 把带类型注解的函数变成工具。用 `Annotated[..., "描述"]` 或
`Annotated[..., Field(description=...)]` 丰富 JSON Schema；
`strict=True` 启用 OpenAI 严格模式。

```python
from typing import Annotated
from lovia import tool

@tool(strict=True)
def search(
    query: Annotated[str, "搜索关键词。"],
    limit: Annotated[int, "返回数量。"] = 5,
) -> list[str]: ...
```

### 友好错误

所有框架异常都带可选 `.hint`；`OutputValidationError` 还会附上模型原文与
目标 schema 名，方便快速定位：

```
OutputValidationError: 2 validation errors for Plan
hint: 可考虑在 Runner.run 上设置 output_repair=True。
raw : '{"steps": "buy ticket"}'
```

---

## 内置工具（按需启用）

全部位于 `lovia.builtins.*`，**不会**被顶层包自动导入。

| 模块 | 内容 |
| --- | --- |
| `lovia.builtins.http`   | `http_fetch` —— `httpx` 的类型化封装 |
| `lovia.builtins.time`   | `now`、`sleep` |
| `lovia.builtins.think`  | `think` —— 思考草稿 |
| `lovia.builtins.fs`     | `FileSystem(root, writable=False)` —— 沙箱化的读写 / 列表 / glob |
| `lovia.builtins.shell`  | `Shell(cwd, needs_approval=True)`（含 `allowlist` 辅助） |
| `lovia.builtins.code`   | `PythonRunner(needs_approval=True)` |
| `lovia.builtins.search` | `web_search(impl=None)` + `WebSearch` Protocol + `DuckDuckGoSearch` |
| `lovia.builtins.todo`   | `TodoList` + `todo_tools(state)` |
| `lovia.builtins.human`  | `HumanChannel` + `ask_human(channel)` |

每个工具的可运行示例见 [`examples/builtins/`](./examples/builtins/)。

---

## 结构化输出

```python
from pydantic import BaseModel
class Answer(BaseModel):
    summary: str
    confidence: float

agent = Agent(name="x", instructions="...", output_type=Answer, output_repair=True)
```

`output_repair=True` 会让模型在首次解析失败时自我修正，通常多一轮调用就能成功。

---

## Skills（`SkillCatalog`）

以 Markdown 驱动的指令包，可选 `references/`、`scripts/`、`assets/` 子目录。
两种模式：

- **lazy**（默认）—— 仅渲染索引，模型按需 `load_skill`。
- **eager** —— 直接把所有 `SKILL.md` 内联进 system prompt。

```python
from lovia.skills import SkillCatalog

catalog = SkillCatalog.from_dir("./skills")
agent = Agent(
    name="Researcher",
    instructions="...",
    tools=catalog.tools(),
)
```

参见 `examples/08_skills.py`。

---

## 示例

| 文件 | 关注点 |
| --- | --- |
| `01_minimal.py`            | Hello world |
| `02_tools.py`              | `@tool` 基础 |
| `03_structured_output.py`  | Pydantic 输出 |
| `04_streaming.py`          | 流式 token |
| `05_handoff.py`            | Agent 之间转交 |
| `06_guardrails.py`         | 输入 / 输出守卫 |
| `07_approval.py`           | 人审介入 |
| `08_skills.py`             | SkillCatalog |
| `09_memory.py`             | 持久上下文 |
| `10_sessions.py`           | 可插拔的会话存储 |
| `11_mcp.py`                | Model Context Protocol |
| `12_tracing.py`            | Hooks & tracing |
| `13_anthropic.py`          | Anthropic provider |
| `14_provider_swap.py`      | 单次调用切换 provider |
| `15_context_policy.py`     | 长对话自动摘要 |
| `16_web.py`                | FastAPI + SSE 聊天 UI |
| `17_dynamic_provider.py`   | 按消息路由 provider |
| `18_hooks.py`              | 生命周期 hooks |
| `19_dynamic_instructions.py` | `@agent.system_prompt` + `append_instructions=` |
| `20_builtins.py`           | 多个 `lovia.builtins.*` 组合 |
| `21_dx.py`                 | `Annotated/Field`、`run_sync`、`Agent.run` |
| `builtins/`                | 每个内置工具一个 demo |

---

## 开发

```bash
pip install -e .[dev]
pytest               # 测试
ruff check .         # lint
ruff format .        # 格式化
mypy lovia           # 类型检查
```

设计哲学与贡献约定见 [`AGENTS.md`](./AGENTS.md)。

## 协议

MIT.
