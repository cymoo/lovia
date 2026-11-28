# 速查表

一页看完 lovia：每个功能一段短代码，并链接到完整指南。把 `<model>` 换成你的端点
提供的模型名（见[配置模型](quickstart.md#2-配置模型)）。用到 `await` 的片段需要在异步
代码里运行；`agent` 指前文任意一个 Agent。

| 从哪里导入 | 里面有什么 |
| --- | --- |
| `lovia` | `Agent`、`Runner`、`tool`、`events`、Session、Checkpoint、各类策略、内置插件 |
| `lovia.tools` | 内置工具：`web_fetch`、`web_search`、`ask_human`、`fence` 等 |
| `lovia.workspace` | `Workspace`、`RemoteHost`、`CommandRule`；规则包在 `lovia.workspace.rules` |
| `lovia.plugins.mcp` | `MCP`、`MCPServerStdio`、`MCPServerStreamableHTTP`（`lovia[mcp]`） |
| `lovia.web` | `create_app`、`default_app`、`serve`、`ChatUI`、`ui_result`（`lovia[web]`） |
| `lovia.testing` · `lovia.eval` | `ScriptedProvider` · `evaluate`、`Case` 和各种检查 |

## 作为库

### Agent

`Agent` 是配置而不是状态：可以随意共享，用 `clone()` 派生变体。指令可以在每次运行时
根据 `ctx.deps` 计算。

```python
from dataclasses import dataclass
from lovia import Agent, RunContext, Runner, tool


@dataclass
class Deps:
    user: str


@tool
def get_order(ctx: RunContext[Deps], order_id: str) -> str:
    """查询当前登录用户的一笔订单。"""
    return f"{ctx.deps.user} 的订单 {order_id}：已发货"


agent = Agent[Deps](
    name="support",
    instructions="回答前先查询订单。",
    model="<model>",
    tools=[get_order],
)


@agent.instruction
def address(ctx: RunContext[Deps]) -> str:
    return f"称呼用户为 {ctx.deps.user}。"


terse = agent.clone(name="support-terse", instructions="用一句话回答。")
result = await Runner.run(agent, "A-1042 到哪了？", deps=Deps(user="小李"))
```

→ [Agent](agents.md)

### 运行与流式输出

脚本里用 `agent.run_sync()`，异步代码里用 `Runner.run()`，要事件流就用
`Runner.stream()`。迭代流的过程中不会抛错：每个流以且仅以一个终止事件结束，
`await handle.result()` 拿到结果。

```python
from lovia import ImagePart, Runner, TextPart, events, user

print(agent.run_sync("天空为什么是蓝色的？").output)

handle = Runner.stream(agent, "光从地球到火星要多久？")
async for ev in handle:
    if isinstance(ev, events.TextDelta):
        print(ev.delta, end="", flush=True)
result = await handle.result()

photo = user([TextPart(text="图里有什么？"), ImagePart(url="https://…/cat.jpg")])
result = await Runner.run(agent, [photo])
```

→ [运行 Agent](running.md) · [流式输出](streaming.md)

### 工具

`@tool` 根据函数签名、docstring、`Annotated` 和 Pydantic `Field` 生成 schema。
同一轮的多个调用并发执行；`parallel=False` 让工具成为屏障。`fence()` 把抓取来的文本
标记为素材而不是指令。

```python
from typing import Annotated
from pydantic import Field
from lovia import tool
from lovia.tools import fence


@tool(strict=True)
def search_docs(
    query: Annotated[str, "搜索关键词"],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> str:
    """搜索内部文档。"""
    return fence(lookup(query, limit))


@tool(parallel=False)
async def apply_migration(name: str) -> str:
    """执行数据库迁移；不和其他工具并发。"""
    return "applied"
```

→ [工具](tools.md) · [内置工具](built-in-tools.md)

### 结构化输出

传入 Pydantic 模型、dataclass、`TypedDict` 或普通类型；最终回答会被校验，解析失败时
默认让模型修复一次。

```python
from pydantic import BaseModel
from lovia import Agent, Runner


class Brief(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(name="summarizer", model="<model>", output_type=Brief)
result = await Runner.run(agent, "总结一下 Transformer 论文。")
result.output.title
```

→ [结构化输出](structured-output.md)

### Provider

不带前缀的模型名走 `OPENAI_BASE_URL` / `OPENAI_API_KEY` 指定的 OpenAI 兼容端点；
`anthropic:` 前缀走 Anthropic（`ANTHROPIC_API_KEY`，其他端点再加 `ANTHROPIC_BASE_URL`）。
也可以传 provider 实例，或实现自己的 `Provider` 协议。

```python
from lovia import Agent, ModelSettings, model_from_env

agent = Agent(
    name="assistant",
    model="anthropic:<model>",
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
agent = Agent(name="assistant", model=model_from_env())  # 读取 LOVIA_MODEL
```

→ [Provider 与模型](providers.md)

### 多 Agent

| 你的任务 | 用法 | 运行方式 |
| --- | --- | --- |
| 让专家接手对话 | `handoffs=[...]` | 专家看到完整历史，在同一次运行里继续 |
| 父 Agent 要先拿到子任务结果 | `agent.as_tool()` | 子 Agent 只看到委派的提示词，回答作为工具结果返回 |
| 独立任务放到后台 | `Subagents([...])` | 子 Agent 独立运行，稍后把报告送回来 |

```python
from lovia import Agent, Subagents

researcher = Agent(name="researcher", instructions="调研并写报告。", model="<model>")

router = Agent(name="router", model="<model>", handoffs=[researcher])
manager = Agent(name="manager", model="<model>",
                tools=[researcher.as_tool(description="调研一个主题。")])
assistant = Agent(name="assistant", model="<model>", plugins=[Subagents([researcher])])
```

→ [多 Agent](multi-agent.md)

### 审批

`permission="ask"` 给工具加上审批。决策可以来自你的 UI、规则或其他渠道；没人回答就拒绝，
所以运行不会挂住，拒绝理由会回到模型。`ev.suspend()` 让运行暂停在这里，从 checkpoint
续跑时会再次询问。

```python
from lovia import Runner, events, tool


@tool(permission="ask")
async def refund(order_id: str, amount_cents: int) -> str:
    """发起退款。"""
    return "refunded"


async for ev in Runner.stream(agent.clone(tools=[refund]), "给订单 A123 退款。"):
    if isinstance(ev, events.ApprovalRequired):
        ev.approve()  # 或 ev.reject("改用店内积分")、ev.suspend()
```

→ [工具审批](tools.md#工具审批)

### Session 与 Checkpoint

Session 让对话跨运行延续。Checkpoint 让单次运行可以从崩溃中恢复，并且幂等：
重新提交已完成的 `run_id` 会直接重放结果，不再调用模型。

```python
from lovia import CheckpointOptions, Runner, SQLiteCheckpointer, SQLiteSession

session = SQLiteSession("chat.db")
await Runner.run(agent, "我的项目叫 Atlas。", session=session, session_id="u1")
await Runner.run(agent, "我的项目叫什么？", session=session, session_id="u1")

cp = SQLiteCheckpointer("runs.db")
await Runner.run(agent, "迁移报表。", checkpoint=CheckpointOptions(cp, "job-42"))
```

→ [Session 与 Checkpoint](sessions-and-checkpoints.md)

### 上下文压缩

压缩只改变下一次模型调用看到的内容，从不改写 transcript，并保持提示词前缀稳定，
让 provider 的缓存持续命中。

```python
from lovia import Agent, Compaction

agent = Agent(
    name="companion",
    model="<model>",
    context_policy=Compaction(context_window=200_000, compact_at=0.85, compact_to=0.60),
)
```

→ [上下文管理](context.md)

### 护栏、重试与预算

护栏在输入/输出边界否决运行。重试策略配置在 Agent 上，限额在每次运行时传入。

```python
from lovia import Agent, RetryPolicy, RunBudget, Runner


async def must_cite(output, ctx):
    if "source:" not in str(output).lower():
        return "缺少来源引用。"  # 返回字符串即触发护栏


agent = Agent(name="researcher", model="<model>",
              output_guardrails=[must_cite], retry=RetryPolicy(max_attempts=2))
await Runner.run(agent, "分析这些日志。",
                 budget=RunBudget(max_tool_calls=20, max_seconds=60))
```

→ [护栏](guardrails.md) · [Provider 重试](retries.md) · [预算与限制](budgets.md)

### Hooks、取消与运行中引导

Hooks 监听每个运行事件（类型与事件流一致）。取消令牌停止一次运行及其子运行；
mailbox 往运行中的任务推一条消息，模型在下一轮看到。

```python
from lovia import AgentHooks, CancelToken, Mailbox, RunContext, Runner, events

hooks = AgentHooks()


@hooks.on(events.TurnStarted)
def last_call(ev, ctx: RunContext):
    if ev.turn == 10:  # 轮次从 1 开始；上限见下面的 max_turns
        ctx.mailbox.push("最后一轮：用已有信息回答。")


token, mailbox = CancelToken(), Mailbox()
handle = Runner.stream(agent.clone(hooks=hooks), "分析这些日志。",
                       max_turns=10, cancel_token=token, mailbox=mailbox)
mailbox.push("重点看 14:00 左右的 5xx 峰值。")  # 下一轮可见
async for ev in handle:
    ...  # 期间可由停止按钮调用：token.cancel("用户点击停止")
```

→ [可观测性](observability.md) · [取消与运行中引导](cancellation.md)

### 工作区

文件和 shell 工具限定在一个根目录下，由同一套 `allow` / `ask` / `deny` 策略管理路径和
命令；`ask` 走上面的审批通道。

```python
from lovia import Agent
from lovia.workspace import CommandRule, RemoteHost, Workspace
from lovia.workspace.rules import DEV_TOOLS, READ_ONLY

agent = Agent(
    name="coder",
    model="<model>",
    workspace=Workspace.local(
        ".",
        mode="coding",  # 或 "readonly"、"trusted"
        command_rules=READ_ONLY + DEV_TOOLS + (CommandRule("gh", "ask"),),
    ),
)
remote = Workspace.remote(RemoteHost.ssh("box"), "/srv/project")  # 工具相同，在远端执行
```

- **模式。** `readonly` 只读根目录、没有 shell；`coding`（默认）可改根目录内的文件、
  读任何地方；`trusted` 哪里都能写，命令不再询问。
- **命令。** `coding` 下没有规则放行的命令都会询问——`READ_ONLY` 和 `DEV_TOOLS` 是现成的
  规则包。不可撤回的命令（`git push`、`rm -r`、`sudo`）带理由询问。
- **底线。** 任何模式下都拒绝危险命令（`git push --force`、`rm -rf /`），凭据文件
  （`~/.ssh` 等）不可访问，写 `.git` 或 CI 配置前先问。
- **后台进程。** `shell(background=true)` 把 dev server、watcher 作为会话拥有的进程运行；
  用 `read_process_output` 读输出，`kill_process` 停止。
- **环境变量。** 命令只继承最小环境，不含 API key 和代理设置；需要的变量用 `env={...}` 传入。
- **编辑。** 文件在上次读取之后被改过，`write_file` 和 `edit_file` 会拒绝改动；
  `grep_files` 跳过 git 忽略的内容。
- **远程。** 容器或 SSH 主机只需要 `python3` ≥ 3.10，策略在那一端执行。

→ [工作区](workspace.md)

### 插件

插件贡献工具、提示词、每轮视图注入、hooks 和护栏，但不接管控制流。Todo、Skills、
MCP 和 Memory 都是插件。

```python
from dataclasses import dataclass
from datetime import datetime
from lovia import Agent, PluginInstance, RunContext, Skills, Todo, tool
from lovia.plugins.mcp import MCP, MCPServerStdio


@tool
def now() -> str:
    """当前本地时间。"""
    return datetime.now().isoformat(timespec="minutes")


@dataclass
class Clock:  # 自己写插件：一个 name 加一个异步 setup()
    name: str = "clock"

    async def setup(self, ctx: RunContext) -> PluginInstance:
        return PluginInstance(tools=[now], instructions="需要时间时调用 `now`。")


agent = Agent(
    name="builder",
    model="<model>",
    plugins=[
        Todo(),                 # 多步任务清单
        Skills("./skills"),     # SKILL.md 指令包，按需加载；"/名字" 点名使用
        MCP(MCPServerStdio(name="fetch", command="uvx", args=["mcp-server-fetch"])),
        Clock(),
    ],
)
```

→ [插件](plugins.md) · [Todo](todo.md) · [Skills](skills.md) · [MCP](mcp.md)

### 记忆

常驻提示词的 **Notes**（只记用户说过或确认过的事实，按会话固定，不破坏缓存）加可检索的
**Archive**。Notes 只由 Agent 自己写入：新事实会替换它取代的旧记录，写满后要先腾出位置才能新增。

```python
from lovia import Agent, Memory, OpenAIEmbedder

agent = Agent(name="assistant", model="<model>", plugins=[Memory("./memory")])

Memory("./memory")                                      # 关键词召回（SQLite FTS5）
Memory("./memory", embedder=OpenAIEmbedder())           # + 语义检索：混合召回
Memory("./memory", index=None)                          # 只有 Notes，不建 Archive
Memory("./memory", scope=lambda ctx: ctx.deps.project)  # + 按项目分开的 Notes 和召回
```

→ [记忆](memory.md)

### 测试与评测

`ScriptedProvider` 用预置的模型回复驱动真实循环和真实工具——离线、确定。`evaluate()`
给行为打分；任何函数都可以是检查，LLM judge 也只是其中一种。

```python
from lovia import Agent, Runner, tool
from lovia.eval import Case, contains, evaluate, llm_judge, tool_called
from lovia.testing import ScriptedProvider, call, text


@tool
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


agent = Agent(name="calc", model="<model>", tools=[add])

# 离线：预置回复驱动真实循环和真实的 `add`。
offline = agent.clone(model=ScriptedProvider([call("add", {"a": 2, "b": 3}), text("5")]))
assert (await Runner.run(offline, "2 + 3 等于多少？")).output == "5"

# 对真实模型：多次采样，给行为打分。
report = await evaluate(agent, [
    Case("法国首都是哪里？", checks=[contains("巴黎")]),
    Case("1234 + 5678 等于多少？", checks=[tool_called("add")]),
    Case("为书店写一句雨天文案", checks=[llm_judge("不超过 30 字，包含雨天和阅读意象")],
         samples=4, pass_threshold=0.75),
])
assert report.passed
```

→ [测试](testing.md) · [评测](eval.md)

## `lovia web`

开箱即用的助手：聊天、工作区工具、记忆、skills、MCP、定时任务和后台子 Agent
（`pip install "lovia[web]"`）。

| 命令 | 作用 |
| --- | --- |
| `lovia web` | 在 `http://127.0.0.1:8000` 启动；新对话在当前目录工作 |
| `lovia web ~/code/app` | 新对话在指定目录工作 |
| `lovia web --home ./.lovia` | 配置、聊天记录和记忆放在这里，而不是 `~/.lovia` |
| `lovia web --readonly` / `--lock-workspace` / `--no-workspace` | 收窄对话能做的事和能去的目录 |
| `lovia web --host 0.0.0.0 --token …` | 在回环地址之外提供服务（必须有 token，不给就自动生成） |
| `lovia web --check` | 探测已配置的模型端点后退出 |
| `lovia web --app app:agent` | 托管你自己的 Agent、`{name: agent}` 映射或应用 |
| `lovia web init-ui ui` | 生成页面定制目录的脚手架 |

- **在设置页里改，不靠命令行参数。** 模型、MCP 服务器、skills、网页搜索，以及助手的指令、
  权限模式和开关都在设置页，从下次运行起生效。
- **按项目生效。** 侧栏按工作区分组对话，多个项目可以并排使用。对话会遵循所在目录的
  `AGENTS.md`（从 git 根目录一路往下）、`.agents/skills` 和 `.mcp.json`；目录也可以在
  SSH 主机上或容器里。
- **输入框。** `@` 引用文件，`/` 列出命令和 skills；每个对话可以单独选目录、权限模式和模型。

→ [Web UI](web-ui.md)

## 在 lovia web 之上构建

三种深度：从“同一个应用，加上我的工具”到“只要 API”。

### 同一个助手，再加上你的工具

`default_app()` 就是 `lovia web` 启动的那个应用；`customize` 把它构建的每个 Agent 交给你。

```python
from lovia import tool
from lovia.web import default_app, serve


@tool
def order_status(order_id: str) -> str:
    """查询订单。"""
    return "shipped"


def customize(agent):
    return agent.clone(tools=[*agent.tools, order_status])


serve(default_app(workspace=".", customize=customize))
```

→ [两个入口](web-server.md#两个入口)

### 你的 Agent，你的页面

`create_app(agent)` 原样托管你构建的 Agent。它返回一个 FastAPI 应用，你自己的路由可以
直接挂在旁边。

```python
from lovia.web import ChatUI, create_app, serve

app = create_app(
    agent,
    title="Acme",
    auth=my_auth,          # 任意 FastAPI 依赖
    run_deps=my_run_deps,  # RunTrigger -> ctx.deps：谁在提问，页面上选了什么
    ui=ChatUI(assets="ui", styles=["theme.css"], logo="logo.svg", scripts=["acme.js"]),
)


@app.get("/acme/stats")
def stats() -> dict: ...


serve(app)
```

工具结果可以渲染成 UI：返回 `ui_result(kind, props)` 即可。内置表格、ECharts 图表和
沙箱 HTML 页面。

```python
from lovia import tool
from lovia.web import ui_result


@tool
def top_products(n: int = 10) -> str:
    """畅销商品，以表格展示给用户。"""
    return ui_result("table", {"columns": ["sku", "units"], "rows": query(n)})
```

页面脚本通过 `window.lovia` 扩展：为自己的 kind 注册渲染器、定制工具卡片、添加斜杠命令，
以及在聊天旁边加整页视图：

```js
lovia.registerRenderer('kpis', { render(el, props) { /* 绘制 props */ } });
lovia.registerToolView('order_status', { summary: (t) => `订单 ${t.args?.order_id}` });
lovia.registerCommand('ticket', {
  description: '创建工单', argument: '标题', run: ({ args }) => openTicket(args),
});
lovia.registerView('dashboard', { title: '仪表盘', render: (el) => drawDashboard(el) });
```

[lovia-react](https://github.com/cymoo/lovia-react) 是一个完整示例：在内置聊天页面上，
用 React 页面作为 lovia 视图扩展。

→ [定制页面](web-server.md#定制页面) ·
[UI 组件](web-server.md#ui-组件) ·
[运行依赖](web-server.md#运行依赖) ·
[认证](web-server.md#认证)

### 只要 API

页面能做的一切都是 JSON + SSE API（`/api/docs`）。关掉内置页面，或者把路由挂进你自己的
FastAPI 应用——单独的路由不带认证，要在前面加上你自己的：

```python
from fastapi import FastAPI
from lovia.web import ChatStore, RouterDeps, build_api_router, create_app

# 方式一：只提供 API，沿用 create_app 的 token / auth 选项。
app = create_app(agent, ui=False)

# 方式二：把路由放进你自己的应用，用你自己的中间件和认证。
deps = RouterDeps(agents={"bot": agent}, store=ChatStore.sqlite("chats.db"))
app = FastAPI(lifespan=deps.lifespan)
app.include_router(build_api_router(deps))
```

```bash
curl -N http://127.0.0.1:8000/api/chat/stream \
  -H 'content-type: application/json' -d '{"message": "你好"}'
```

→ [HTTP API](http-api.md) · [生产部署](deployment.md)
