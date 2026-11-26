# lovia

[![PyPI](https://img.shields.io/pypi/v/lovia)](https://pypi.org/project/lovia/)
[![Python](https://img.shields.io/pypi/pyversions/lovia)](https://pypi.org/project/lovia/)
[![CI](https://github.com/cymoo/lovia/actions/workflows/ci.yml/badge.svg)](https://github.com/cymoo/lovia/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/cymoo/lovia/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-cymoo.github.io%2Flovia-black)](https://cymoo.github.io/lovia/zh/)

[English](https://github.com/cymoo/lovia/blob/main/README.md) | **简体中文**

**同一个 Agent 循环，三种用法。** lovia 是一个 Python Agent 框架：可以作为库导入，
可以作为浏览器里开箱即用的助手直接运行，也可以拆开来搭建你自己的产品。每一层都建在下一层
之上，从一个脚本走到一个产品，不用换框架。核心只依赖 `httpx`、`pydantic` 和 `pyyaml`，
其余能力都按需安装。

| 把 lovia 用作… | 你得到什么 | 从这里开始 |
| --- | --- | --- |
| [**库**](#作为库) | 用普通 Python 写的 Agent 循环：工具、流式输出、会话、审批、上下文管理 | `pip install lovia` |
| [**开箱即用的 harness**](#作为开箱即用的-harnesslovia-web) | 一个本地助手，直接在你的项目目录里干活：文件、shell、记忆、skills、MCP | `pip install "lovia[web]"`，然后 `lovia web` |
| [**自定义 harness 的底座**](#作为自定义-harness-的底座) | 把这个助手当作工具箱：加工具、换品牌、加视图，或者只用它的 API | `lovia[web]`，然后 `default_app()` 或 `create_app()` |

![lovia web 界面](https://raw.githubusercontent.com/cymoo/lovia/main/docs/images/web-ui.webp)

*`lovia web`：侧栏按工作区分组对话，工具画出的图表，输入框上方是 Agent 的计划，右侧是它刚更新的报告的 diff。*

## 作为库

```bash
pip install lovia
export OPENAI_BASE_URL=https://your-endpoint/v1  # 任何 OpenAI 兼容端点
export OPENAI_API_KEY=...
```

```python
from lovia import Agent, tool


@tool
def get_order(order_id: str) -> str:
    """根据订单号查询订单状态。"""
    return f"订单 {order_id}：两天前已发货，预计周四送达。"


agent = Agent(
    name="support",
    instructions="回答前先查询订单，用一两句话回复。",
    model="glm-5.2",  # 你的端点提供的模型
    tools=[get_order],
)

print(agent.run_sync("我的订单 A-1042 到哪了？").output)
```

异步代码里用 `await Runner.run(agent, ...)`，需要类型化事件流就用 `Runner.stream(...)`。
使用 Anthropic 时，设置 `ANTHROPIC_API_KEY` 并写 `model="anthropic:<model>"`
（见[配置模型](https://cymoo.github.io/lovia/zh/quickstart/#2-配置模型)）。

循环之上的能力，每一项都只需几行代码：

- [**工具**](https://cymoo.github.io/lovia/zh/tools/)来自带类型的函数，并发执行；
  [**结构化输出**](https://cymoo.github.io/lovia/zh/structured-output/)直接校验成 Pydantic 模型。
- [**多 Agent**](https://cymoo.github.io/lovia/zh/multi-agent/)：handoff、agent-as-tool，
  或者在后台运行、稍后汇报的子 Agent。
- [**审批**](https://cymoo.github.io/lovia/zh/tools/#工具审批)：敏感工具的调用可以由任意 UI
  决定，也可以挂起运行，等有人回来再答。
- [**Session 与 Checkpoint**](https://cymoo.github.io/lovia/zh/sessions-and-checkpoints/)：
  对话持久化，崩溃的运行从中断处续跑。
- [**上下文压缩**](https://cymoo.github.io/lovia/zh/context/)：从不改写 transcript，
  让 provider 的提示词缓存持续命中。
- [**工作区**](https://cymoo.github.io/lovia/zh/workspace/)：文件和 shell 工具由同一套
  路径与命令策略管理，可以在本地、SSH 主机或容器里。
- [**插件**](https://cymoo.github.io/lovia/zh/plugins/)：Skills、MCP、Todo 和长期记忆，
  用的就是你也能用的那套扩展机制。
- [**护栏**](https://cymoo.github.io/lovia/zh/guardrails/)、
  [**重试与预算**](https://cymoo.github.io/lovia/zh/budgets/)、
  [**取消与运行中引导**](https://cymoo.github.io/lovia/zh/cancellation/)，
  以及[**离线测试**](https://cymoo.github.io/lovia/zh/testing/)和[**评测**](https://cymoo.github.io/lovia/zh/eval/)。

**→ [速查表](https://cymoo.github.io/lovia/zh/cheatsheet/)**：每个功能一段代码 ·
[快速开始](https://cymoo.github.io/lovia/zh/quickstart/) ·
[示例](https://github.com/cymoo/lovia/tree/main/examples)：按编号排列、可直接运行的脚本

## 作为开箱即用的 harness：`lovia web`

```bash
pip install "lovia[web]"
lovia web
```

打开 `http://127.0.0.1:8000`，首次启动时添加一个模型。新对话在启动目录里工作；
`lovia web ~/code/app` 可以指定其他目录。

- **在你的目录里干活。** 读写文件、执行命令、用 todo 清单规划、搜索网页（经托管搜索服务，
  默认 Parallel，无需 key；可在设置 → 联网搜索中更换或关闭）、画图表和表格，
  把独立的工作交给后台子 Agent。后台在跑什么、命令输出了什么、子任务做到哪一步和最后的报告，都在右侧面板里看。
- **同时处理多个项目。** 侧栏按工作区分组对话。每个对话有自己的目录、模型和权限模式，
  目录也可以在 SSH 主机上或容器里。问助手你有哪些工作区、之前某个对话聊了什么，它会自己去查。
- **始终由你掌控。** 权限模式从只读到完全访问。默认模式下，不可撤回的命令会先询问并说明
  原因，批准可以在本对话内记住；任何模式下，危险命令都会被拒绝。
- **遵循项目自己的约定。** 对话会加载所在目录的 `AGENTS.md`、`.agents/skills` 和
  `.mcp.json`。输入框里 `/` 列出命令和 skills，`@` 引用文件。
- **完整的聊天体验。** 流式输出、从任一回复分支、运行中插话、附件、定时任务，以及可编辑的
  长期记忆。关掉标签页，运行也会继续。还可以开启语音输入，用你选的转写服务把说的话变成文字。
- **设置不用重启。** 模型、MCP 服务器、skills 和助手的指令都在设置页里改。所有数据都放在
  一个 home 目录 `~/.lovia` 里，从不放进你的仓库。

在 `127.0.0.1` 上无需认证；绑定其他地址时必须使用 token。完整参数见 `lovia web --help`。

**→ [Web UI 指南](https://cymoo.github.io/lovia/zh/web-ui/)**

## 作为自定义 harness 的底座

`lovia web` 由一组公开的部件组装而成。按需要选择深度：

| 你想要 | 用法 | 保留什么 |
| --- | --- | --- |
| 同一个助手，再加上你的工具 | `default_app(customize=...)` | `lovia web` 的全部：设置页、记忆、工作区、定时任务 |
| 你自己的 Agent，放进换了品牌、加了扩展的聊天页面 | `create_app(agent, ui=ChatUI(...))` | 聊天页面：流式输出、审批、历史、文件 |
| 你自己的前端 | `create_app(agent, ui=False)` 或 `build_api_router()` | 页面背后的 JSON + SSE API（`/api/docs`） |

**同一个助手，再加上你的工具。** `default_app()` 构建的正是 `lovia web` 启动的应用，
`customize` 把它构建的每个 Agent 交给你扩展：

```python
from lovia import tool
from lovia.web import default_app, serve


@tool
def order_status(order_id: str) -> str:
    """查询订单。"""
    return "shipped"


serve(default_app(customize=lambda agent: agent.clone(tools=[*agent.tools, order_status])))
```

**你的 Agent，你的页面。** `create_app(agent)` 原样托管你构建的 Agent。它返回一个
FastAPI 应用，你自己的路由可以直接挂在旁边：

```python
from lovia.web import ChatUI, create_app, serve

app = create_app(
    agent,
    title="Acme",
    auth=my_auth,          # 任意 FastAPI 依赖
    run_deps=my_run_deps,  # 谁在提问、页面上选了什么 -> ctx.deps
    ui=ChatUI(assets="ui", styles=["theme.css"], logo="logo.svg", scripts=["acme.js"]),
)
serve(app)
```

- **能渲染 UI 的工具结果。** 工具返回 `ui_result(kind, props)` 即可。内置表格、ECharts
  图表和沙箱 HTML 页面。
- **可脚本化的页面。** 你的脚本通过 `window.lovia` 注册渲染器、工具卡片视图、斜杠命令，
  以及聊天旁边的整页视图。模板 block、设计 token 和 `data-i18n` 键分别用来改页面的结构、
  外观和文案。`lovia web init-ui ui` 生成脚手架目录。
- **可运行的示例。** 示例 `26`–`27` 和 `31`–`35` 覆盖托管、组件、品牌定制和扩展脚本。
  [lovia-react](https://github.com/cymoo/lovia-react) 演示了用 React 视图扩展内置聊天页面。

**你自己的前端。** `create_app(agent, ui=False)` 只提供 API；`build_api_router()` 把它挂进
你自己的 FastAPI 应用，认证由你自己负责。

**→ [Web 服务端指南](https://cymoo.github.io/lovia/zh/web-server/)** ·
[HTTP API](https://cymoo.github.io/lovia/zh/http-api/) ·
[生产部署](https://cymoo.github.io/lovia/zh/deployment/)

## 设计原则

- **简洁。** 不可变的 `Agent`、无状态的 `Runner`、`@tool`，以及作为唯一扩展轴的插件。
  每个部件的心智模型都不超过一屏。
- **轻量。** 只有三个硬依赖。MCP、Web 应用和搜索后端都是 extra，用到时才导入。
- **读得懂。** 模型调用、工具执行、重试和持久化都在一个显式的循环里。遇到意外行为，
  顺着一条链查下去就行。
- **可扩展，但克制。** 扩展点都是 dataclass、Protocol 和装饰器，从不靠继承。能在应用层
  用几行代码搞定的，就不做进框架。

## 安装

lovia 需要 Python 3.10 或更新版本。

| 需求 | 安装 |
| --- | --- |
| 核心库 | `pip install lovia` |
| MCP | `pip install "lovia[mcp]"` |
| Web 应用（含 MCP） | `pip install "lovia[web]"` |
| DuckDuckGo 搜索 | `pip install "lovia[ddg]"`（Tavily 无需额外依赖，设置 `TAVILY_API_KEY` 即可） |
| 可运行示例 | `pip install "lovia[examples,web]"` |

## 项目状态

lovia 处于 beta 阶段，正在走向 1.0。在此之前，破坏性变更可能出现在补丁版本里；如果这对你
很重要，请固定版本。1.0 将承诺保持稳定的范围见[稳定性](https://cymoo.github.io/lovia/zh/stability/)。

## 参与开发

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format .
.venv/bin/python -m mypy lovia
```

真实端点测试默认跳过，开启方式：
`LOVIA_LIVE_TESTS=1 .venv/bin/python -m pytest -m live_provider`。修改内部实现之前，请先读
[架构说明](https://cymoo.github.io/lovia/zh/architecture/)。

## 许可证

[MIT](https://github.com/cymoo/lovia/blob/main/LICENSE)
