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
- 🛡️ **生产级原语** — 护栏、审批门控、生命周期钩子、策略化的文件/Shell 工具、可插拔功能（todo 清单或你自己的插件）——需要时都在，用不到时不存在。

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

长对话默认使用 `Compaction` 管理上下文。压缩是**仅作用于单次调用的视图且具有粘性**：
它只裁剪发送给模型的那一份转录，绝不修改已存储的 session，因此完整历史始终是唯一可信源；
同时压缩决策在单次 run 内被记住并逐调用重放，prompt 前缀跨轮保持字节级稳定——provider 的
prompt cache 不会失效。当 token 压力到来时按"先便宜后昂贵"依次执行：超大工具结果归档到
workspace 文件（有 workspace 时）、较旧工具结果替换为简短 recall 标记、最后才把更早的前缀
折叠进增量式 LLM 摘要。压缩以稀疏突发方式发生——低于 `compact_at` 水位线时什么都不做，
触发后一次性压到 `compact_to` 水位线。

如果你想调整阈值或阶段组合，可以显式传入 policy：

```python
from lovia import Compaction

policy = Compaction(
    context_window=200_000,  # 省略则向 provider 询问
    compact_at=0.75,         # 可用窗口的 75% 时开始压缩
    compact_to=0.50,         # 一次压到 50%（也支持绝对 token 数，如 100_000）
)
result = await Runner.run(agent, "继续。", context_policy=policy)
```

压缩限制的是**模型看到的视图**；transcript 本身保留完整工具输出（这正是 recall 与
view-only 安全性的前提）。对可能返回巨型输出的工具，应在源头限制进入 transcript 的体量——
`Agent(max_tool_output_chars=...)` 或工具级 `@tool(max_output_chars=...)` 会在存储前截断
超长输出（保留头尾 + 标记），同时丢弃 raw 原始对象，从而约束内存、checkpoint 与 session
的开销。内置 workspace 工具已通过 `Workspace` 的限制在源头封顶。

可加入可选的 `recall_tool_result` 工具，让 agent 在压缩丢弃了某个工具输出后，无需重跑
工具即可取回完整结果：

```python
from lovia.tools import recall_tool_result

agent = Agent(name="x", tools=[..., recall_tool_result])
```

如果需要完全禁用自动上下文管理，传入 `NoopContextPolicy()`。

## Skills（技能库）

遵循
`Agent Skills 规范 <https://agentskills.io/specification>`_
的可复用指令包。渐进式披露让上下文窗口保持精简：metadata 始终可见，
完整指令和子文件通过工具调用按需加载。

```python
from lovia import Agent, Skills

agent = Agent(
    name="support",
    model="deepseek-v4-pro",
    skills=Skills.from_dir("./skills"),
)
```

每个 skill 是一个包含 ``SKILL.md``（YAML frontmatter + body）的目录。
可选的 ``references/``、``scripts/``、``assets/`` 子目录存放补充资源，
模型通过 ``read_skill_file`` 按需加载。

可传入多个目录合并技能库——``Skills.from_dir("./skills", "./team-skills")``
（同名时先出现者优先）。frontmatter 中除 ``name``/``description`` 之外的额外字段
（``tags``、``version`` 等）会一并展示在索引里，便于模型路由。Body 按需懒加载，
不常驻内存。

通过 ``filter`` 谓词限定暴露哪些技能——适合按租户或权限划分的技能库。被过滤掉的技能
既不出现在索引中，也无法被加载::

    Skills.from_dir("./skills", filter=lambda m: "internal" not in m.extra.get("tags", []))

自定义 skill 来源（数据库、API、MCP）实现 ``SkillSource`` 协议即可。

## 插件（Plugins）

**插件**把一个功能所需的工具、每轮上下文、系统提示词、事件钩子打包成一个对象，
每次运行时新建一份。一行挂到 Agent 上，不必把各部分分别接线。

内置的 **todo 插件**给模型一个 `todo_write` 工具，并在每一轮把当前清单回显给它，
让它在多步、长任务中始终不跑偏：

```python
from lovia import Agent, Runner, todo_plugin

agent = Agent(
    name="builder",
    instructions="认真完成多步骤任务。",
    model="deepseek-v4-pro",
    plugins=[todo_plugin()],
)
await Runner.run(agent, "搭一个 REST API：数据模型、增删改查、测试、文档。")
```

每轮注入的提醒是**仅视图（view-only）**的——只进入当次模型调用，绝不写入 transcript
或 session，所以轮数再多上下文也不会膨胀。而每次 `todo_write` 调用本身仍留在
transcript 里（结果带结构化 `list[Todo]`），天然形成审计轨迹，并支持 resume/handoff
自动恢复。是否用清单由模型自己判断：琐碎任务不会生成清单，零开销。

通过过滤事件实时查看进度：

```python
from lovia import events

async for ev in Runner.stream(agent, task):
    if isinstance(ev, events.ToolCallCompleted) and ev.call.name == "todo_write":
        for t in ev.result:                # list[Todo]
            print(t.status, "-", t.content)
```

自己写一个插件，只需 `setup()` 返回它贡献的内容：

```python
from lovia import InputEntry
from lovia.plugins import PluginInstance

class StayTerse:
    name = "stay_terse"

    def setup(self) -> PluginInstance:
        def remind(ctx):
            return [InputEntry(role="user", content="<reminder>保持简洁。</reminder>")]
        # PluginInstance 还可携带：tools、instructions、hooks。
        return PluginInstance(view_injectors=[remind])
```

`view_injectors` 每轮运行，把临时条目追加到当次模型调用——这是临时提醒/临时插入消息
的通用机制。

## 内置工具

实用工具统一放在 `lovia.tools` 下，不会自动导入，按需取用：

```python
from lovia.tools.http import http_fetch
from lovia.tools.search import duckduckgo_search_tool
from lovia.tools.todo import TodoList, todo_tools
from lovia.tools.human import HumanChannel, ask_human
from lovia.tools.time import now

todos = TodoList()
agent = Agent(
    name="assistant",
    model="deepseek-v4-pro",
    tools=[
        http_fetch,
        duckduckgo_search_tool(),
        *todo_tools(todos),
        now,
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

## MCP

连接 [Model Context Protocol](https://modelcontextprotocol.io) 服务器，其工具会
以普通 lovia 工具的形式出现。支持两种传输：`MCPServerStdio`（子进程）与
`MCPServerStreamableHTTP`（远程端点）。

```bash
pip install "lovia[mcp]"
```

```python
from lovia import Agent
from lovia.mcp import MCPServerStdio

agent = Agent(
    name="assistant",
    model="openai:gpt-5.4",
    mcp_servers=[
        # 官方 `fetch` 服务器，可从公开 Web API 拉取实时数据。
        MCPServerStdio(
            name="web",                      # 工具名前缀：web__fetch
            command="uvx",
            args=["mcp-server-fetch"],
        )
    ],
)
```

默认情况下每次运行都会新建连接并在结束后关闭（对并发运行天然安全）。若想在多次运行
间复用同一连接，打开一个 **session** 并把活动连接挂到 agent 上：

```python
server = MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])

async with server.session() as conn:          # 打开一次，反复复用
    agent = Agent(name="assistant", mcp_servers=[conn])
    await Runner.run(agent, "Fetch https://wttr.in/Tokyo?format=j1 and summarise it.")
    await Runner.run(agent, "...")
    tools = await conn.refresh_tools()         # 服务器有变动时重新列举
```

完整的流式示例见 `examples/26_mcp.py`。

要点：

- **过滤** — `include_tools` / `exclude_tools`（按 MCP 原始工具名匹配）。
- **结果** — 文本原样透传；图片/音频/二进制渲染为紧凑占位符
  （`[image: image/png, 12.3 KB]`），绝不塞入 base64。传入 `result_renderer`
  可拿到原始 `MCPToolResult`，自行决定喂给模型的内容。返回 MCP `isError` 的工具会
  以 `[tool error] …` 标记展示给模型，便于其自我纠正。
- **韧性** — 传输错误统一包装为 `MCPError`；`auto_reconnect`（默认开启）会在单次
  调用内透明地重连一次。对于 `stdio`，重连即重启进程，服务器端状态会丢失。

明确不做：MCP prompts、resource 浏览、sampling、OAuth、hosted MCP——以保持接口精简。

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
| `examples/27_todos.py` | todo 插件 / 任务清单 |
| `examples/16_web_serve.py` | Web UI |
| `examples/22_sandbox.py` | 直接使用 sandbox session |
| `examples/23_sandbox_agent.py` | Coding Agent |
| `examples/26_mcp.py` | 远程 MCP 服务器（fetch）+ 流式 |
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
