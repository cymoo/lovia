# lovia

一个不挡路的 Python Agent 框架。

```bash
pip install lovia
```

```python
# 在环境变量或 .env 中配置一次：
# OPENAI_BASE_URL=https://api.deepseek.com
# OPENAI_API_KEY=sk-your-key

import asyncio
from lovia import Agent, Runner, tool


@tool
async def add(a: int, b: int) -> int:
    """把两个数相加。"""
    return a + b


async def main() -> None:
    agent = Agent(
        name="calc",
        instructions="简短回答，需要时调用工具。",
        model="deepseek-v4-pro",
        tools=[add],
    )
    result = await Runner.run(agent, "2 + 3 等于几？")
    print(result.output)  # 5


asyncio.run(main())
```

---

## 为什么是 lovia？

LLM Agent 框架不少，lovia 的取舍如下：

- 🪶 **概念极简** — Agent、Runner、tool，整个心智模型一页纸讲完。
- 🔌 **模型中立** — OpenAI、Anthropic、任何 OpenAI 兼容接口，一行代码切换。
- 🧩 **扩展无需继承** — 全程 Protocol 和 dataclass，自定义 session store、memory 或 provider，不用动框架内部。
- ✂️ **默认极轻** — 只有 `httpx` 和 `pydantic` 是必须的，Web UI、MCP、搜索和编排全是可选项。
- 🛡️ **生产级原语** — 护栏、审批门控、生命周期钩子、沙箱化的文件/Shell 工具——需要时都在，用不到时不存在。

---

## 定义 Agent

`Agent` 是普通的 dataclass，不需要继承任何基类：

```python
from lovia import Agent

agent = Agent(
    name="writer",
    instructions="回答要简洁、有说服力。",
    model="deepseek-v4-pro",
)
```

动态系统提示片段可以在运行时注入：

```python
@agent.system_prompt
async def add_context(ctx) -> str:
    return f"用户等级：{ctx.context['tier']}"
```

需要临时变体？克隆一份，原始 agent 不受影响：

```python
strict = agent.clone(instructions="必须引用来源。", output_type=Report)
```

## Runner

```python
from lovia import Runner

result = await Runner.run(agent, "写一段 release note。")
print(result.output)
```

流式输出实时返回类型化事件：

```python
from lovia import events

handle = Runner.stream(agent, "讲一个短故事。")
async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)

result = await handle.result()
```

脚本场景用同步包装器：

```python
result = Runner.run_sync(agent, "帮我总结一下。")
```

## 工具

任意带类型注解的 Python 函数都能成为工具。lovia 会自动从类型注解、docstring 和
`Annotated`/`Field` 元数据生成 JSON Schema：

```python
from typing import Annotated
from pydantic import Field
from lovia import tool


@tool
async def fetch_weather(city: str) -> str:
    """查询某个城市的当前天气。"""
    ...


@tool(strict=True)
async def search_docs(
    query: Annotated[str, Field(description="搜索关键词")],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> list[str]:
    """搜索内部文档。"""
    ...
```

### 工具审批

敏感工具可以要求在执行前得到明确批准：

```python
@tool(needs_approval=True)
async def delete_record(record_id: str) -> str:
    """永久删除一条记录。"""
    ...
```

程序化审批（适合自动化流水线）：

```python
agent = Agent(
    ...,
    approval_handler=lambda call, ctx: call.name != "delete_record",
)
```

流式模式下 Runner 发出 `ApprovalRequired` 事件，由你的 UI 来决定：

```python
async for ev in handle:
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()   # 或 ev.deny("原因")
```

## 结构化输出

传入 Pydantic 模型即可得到校验后的类型化输出：

```python
from pydantic import BaseModel


class Summary(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(
    name="summarizer",
    model="deepseek-v4-pro",
    output_type=Summary,
)
result = await Runner.run(agent, "用三条要点总结 lovia。")
print(result.output.title)
```

每次调用可以临时覆盖输出类型，不影响 agent 配置：

```python
result = await Runner.run(agent, "给我一个 JSON 摘要。", output_type=Summary)
```

## 多 Agent：Handoff 与组合

### Handoff（移交控制权）

分诊 agent 把请求无缝路由到专项 agent：

```python
from lovia.handoff import Handoff, drop_stale_tool_calls

billing = Agent(name="billing", instructions="处理账单问题。", model="deepseek-v4-pro")
support = Agent(name="support", instructions="处理技术故障。", model="deepseek-v4-pro")

triage = Agent(
    name="triage",
    instructions="把问题路由到合适的专项 agent。",
    model="deepseek-v4-pro",
    handoffs=[
        Handoff(target=billing, input_filter=drop_stale_tool_calls),
        Handoff(target=support, input_filter=drop_stale_tool_calls),
    ],
)

result = await Runner.run(triage, "我被重复扣款了。")
```

### Agent 作为工具

把 agent 包装成工具，让父级 agent 把子任务委托出去：

```python
summarizer = Agent(name="summarizer", instructions="总结文本。", model="deepseek-v4-pro")

orchestrator = Agent(
    name="orchestrator",
    model="deepseek-v4-pro",
    tools=[summarizer.as_tool(description="总结一段文本。")],
)
```

子 agent 在独立子循环中运行，最终输出作为工具调用结果返回。

## Human in the loop

### 审批门控

给工具设置 `needs_approval=True`，Runner 会暂停执行，直到审批通过或被拒绝——
由流式消费者、Web handler 或 agent 的 `approval_handler` 来决定。

### 主动提问

`ask_human` 让模型在需要时显式向操作员请求输入：

```python
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()
agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    tools=[ask_human(channel)],
)

# 在你的 UI 或事件循环中响应：
for q in channel.pending:
    channel.answer(q.id, "请选择方案 A。")
```

## Hooks（生命周期钩子）

`AgentHooks` 在运行各阶段触发，适合日志、监控、调试：

```python
from lovia.hooks import AgentHooks
from lovia import events

hooks = AgentHooks()

@hooks.on(events.ToolCallStarted)
async def log_tool(ev):
    print(f"→ {ev.call.name}({ev.call.arguments})")

@hooks.on((events.RunCompleted, events.ErrorOccurred))
def at_end(ev):
    print("结束：", type(ev).__name__)

agent = Agent(..., hooks=hooks)
```

Handler 可以是同步或异步函数，两者都支持。

## Guardrails（护栏）

在运行前（input）或结束后（output）执行检查的异步函数：

```python
from lovia.exceptions import GuardrailTripped


async def no_pii(messages, ctx):
    for m in messages:
        if "@" in str(m.content):
            raise GuardrailTripped("检测到个人信息——输入中包含邮箱地址。")


async def must_cite(output, ctx):
    if "来源：" not in output:
        return "回答中必须包含引用来源。"  # 返回非空字符串表示违规


agent = Agent(
    name="researcher",
    model="deepseek-v4-pro",
    input_guardrails=[no_pii],
    output_guardrails=[must_cite],
)
```

返回 `None` 或 `False` 表示检查通过。

## 会话与记忆

跨多次调用保留对话上下文：

```python
from lovia.stores import SQLiteSession

session = SQLiteSession("chat.db")
await Runner.run(agent, "我的项目叫 Atlas。", session=session, session_id="u1")
await Runner.run(agent, "我的项目叫什么？",  session=session, session_id="u1")
```

长对话接近上下文窗口上限时，context policy 会自动压缩旧消息：

```python
from lovia import SummarizingContextPolicy

policy = SummarizingContextPolicy(keep_recent_messages=10)
result = await Runner.run(agent, "继续。", context_policy=policy)
```

## Skills（技能库）

按需加载的文件驱动提示片段——适合不需要一直占用上下文窗口的大型领域知识：

```python
from lovia.skills import SkillCatalog

catalog = SkillCatalog("skills/", mode="lazy")   # 或 mode="eager"

agent = Agent(
    name="support",
    model="deepseek-v4-pro",
    skills=catalog,
)
```

每个 skill 是一个目录，包含带 YAML frontmatter 的 `SKILL.md`。
`lazy` 模式下模型按需调用 `load_skill(name)`；`eager` 模式下所有 skill 在启动时内联。

## 内置工具

实用工具统一放在 `lovia.tools` 下，不会自动导入，按需取用：

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
    model="deepseek-v4-pro",
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

## Sandbox 与 Coding Agent

给 agent 挂载 sandbox，无需手动拼装每个文件工具：

```python
from lovia import Agent
from lovia.sandbox import Sandbox

agent = Agent(
    name="coder",
    instructions="做精准、有限的代码修改。",
    model="deepseek-v4-pro",
    sandbox=Sandbox.local(".", mode="coding"),
)
```

| 模式 | 可用工具 |
| --- | --- |
| `"readonly"` | read\_file、list\_dir、glob |
| `"coding"` | read\_file、write\_file、edit\_file、list\_dir、glob + shell（需审批） |
| `"trusted"` | 以上全部，shell 无需审批 |

本地 sandbox 只接受相对路径，拒绝绝对路径、`..` 逃逸和符号链接逃逸。
注意：本地 shell 仍以当前系统用户执行，这是便利边界，不是强安全沙箱。

也可以直接使用工具 factory：

```python
from lovia.tools import coding_tools

agent = Agent(
    name="coder",
    model="deepseek-v4-pro",
    tools=coding_tools(root=".", mode="coding"),
)
```

## Web UI

一行代码启动带流式输出的聊天界面：

```bash
pip install "lovia[web]"
python examples/16_web_serve.py
```

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

特性：SSE 流式输出 · 持久化会话 · 工具 HTTP 审批 · 安全 Markdown 渲染 · Jinja2 零构建页面。

## 示例索引

| 文件 | 内容 |
| --- | --- |
| `examples/01_hello.py` | 最小 Agent |
| `examples/02_tools.py` | 自定义 `@tool` |
| `examples/03_streaming.py` | Rich 流式输出 |
| `examples/04_structured_output.py` | Pydantic 结构化输出 |
| `examples/05_handoff.py` | Agent handoff |
| `examples/08_skills.py` | Skill 技能库 |
| `examples/11_approval.py` | 工具审批 |
| `examples/16_web_serve.py` | Web UI |
| `examples/22_sandbox.py` | 直接使用 sandbox session |
| `examples/23_sandbox_agent.py` | Coding Agent |
| `examples/24_prefect.py` | Prefect 工作流 |
| `examples/tools/` | 各工具专项示例 |
| `examples/workflows/` | 常见工作流模式 |

## 开发

```bash
pip install -e ".[dev]"

ruff check .          # lint
ruff format .         # 格式化
mypy lovia            # 类型检查
pytest -q             # 运行测试
```

## 安装 extras

| 需求 | 安装 |
| --- | --- |
| 核心框架 | `pip install lovia` |
| DuckDuckGo 搜索工具 | `pip install "lovia[tools]"` |
| MCP 集成 | `pip install "lovia[mcp]"` |
| Web UI | `pip install "lovia[web]"` |
| Prefect 工作流 | `pip install "lovia[prefect]"` |
| 运行所有示例 | `pip install "lovia[examples,web]"` |
| 开发 / CI | `pip install -e ".[dev]"` |
