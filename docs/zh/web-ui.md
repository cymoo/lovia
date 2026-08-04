# Web UI

Web UI 是 lovia 自带的本地聊天应用，适合直接使用 Agent，或在开发自定义前端前验证功能。

## 一条命令启动

```bash
pip install "lovia[web]"
lovia web
```

访问 `http://127.0.0.1:8000`，首次启动时配置一个模型即可。之后可用
`lovia web --check` 检查配置并探测端点，而不启动服务。

模型配置有两级，项目级文件存在时会**整体覆盖**用户级配置，而不是逐项合并：

| 路径 | 作用域 |
| --- | --- |
| `~/.lovia/config.json` | 当前用户的默认配置 |
| `./.lovia/config.json` | 当前项目的完整覆盖配置 |

密钥和聊天数据所在的 `.lovia/` 会自动加入 `.gitignore`。在支持 Unix 权限的平台上，
配置文件只允许当前用户读写。

CLI 创建的默认 Agent 包含 Todo、Memory、时间与 HTTP Tool、Web 搜索、定时任务，以及以
当前目录为根的 coding 模式 Workspace。它还会自动读取：

- `AGENTS.md`：作为 Agent instructions；
- `./.agents/skills`：作为 Skills 目录；
- `./.lovia/memory`：作为 Memory 目录。

旧的 `./skills` 不再自动加载；可以迁移到 `./.agents/skills`，或显式传入
`--skills-dir skills`。Web 搜索在配置 Tavily Key 后使用 Tavily，否则尝试可选的
DuckDuckGo 后端。

默认功能会产生主 Run 之外的模型调用：Memory 默认在每个已完成 Run 后提炼一次，定期整理时
再调用一次；标题和追问建议也各自独立调用。可以用 `--no-memory`、`--no-followups` 关闭对应功能，
或把辅助任务指派给更便宜的模型。Memory 的完整成本边界见[记忆](memory.md#使用建议)。

!!! danger "默认仅供本机使用"

    回环地址 `127.0.0.1` 默认不鉴权。绑定其他地址时必须设置 `--token` 或
    `LOVIA_WEB_TOKEN`；未设置时服务会自动生成。持有 token 的客户端可以使用 Agent 的
    全部能力，包括编辑文件和执行 Shell 命令，因此应把 token 当作密码。允许其他设备访问时，
    建议同时使用 `--readonly`；多用户部署请参阅[生产部署](deployment.md)。

## 模型配置与切换

模型档案保存在 `config.json` 中。连接测试会真实检查端点可达性、认证、模型列表和上下文窗口；
端点未列出的模型 ID 仍可手动填写。

API Key 只写不读：服务端只返回是否已设置及脱敏提示，不会把完整密钥传回浏览器。配置更新会
整体校验、原子写入并立即生效，无需重启。

切换模型从**下一条消息**开始生效，包括旧对话的后续消息、定时任务和后台子 Agent；已经开始的
回复仍使用原模型。视觉理解以及标题、追问建议等辅助任务，可以分别指派给其他模型档案。

搜索后端与 Tavily Key 也保存在 `config.json` 中。模型连接、多模型档案、角色指派和搜索配置
没有对应的 CLI 参数；请通过设置页或直接维护配置文件。

## 加载自定义 Agent

创建 `app.py`：

```python
from lovia import Agent

assistant = Agent(
    name="assistant",
    instructions="清晰回答，并在有助于提高准确性时使用工具。",
    model="<model>",
)
```

然后运行：

```bash
lovia web --app app:assistant
```

`--app MODULE:ATTR` 接受一个 Agent，或 `{name: agent}` 映射。Python 部署和 ASGI 集成详见
[Web 服务端](web-server.md)。

界面支持 GitHub 风格 Markdown、代码高亮、Mermaid 和内嵌图片。默认 Agent 已知道这些能力；
自定义 Agent 不会自动获得相关提示。如果希望模型主动使用这些格式，可以加入 `SURFACE_NOTE`：

```python
from lovia.web import SURFACE_NOTE

assistant = Agent(
    name="assistant",
    instructions="清晰回答。\n\n" + SURFACE_NOTE,
    model="<model>",
)
```

## 图片与文件

附件会写入 Workspace 的 `uploads/`，消息中保存的是相对路径。因此，即使模型不能直接读取附件，
Agent 仍可通过 Workspace Tool 打开文件。`--no-workspace` 会同时关闭这条路径。

图片是否内联发送由视觉配置决定：

- 官方 OpenAI 和 Anthropic 端点默认按支持视觉处理；其他视觉端点需要声明视觉能力，或设置
  `LOVIA_VISION=1`。
- 纯文本主模型可以把视觉角色指派给另一个模型。使用环境变量配置时，对应项是
  `LOVIA_VISION_MODEL=<vendor>:<model>`；独立端点还可设置 `LOVIA_VISION_BASE_URL` 和
  `LOVIA_VISION_API_KEY`。此时主模型通过 `see_image` Tool 获得文字结果，图片原始数据不会
  写入主模型的对话历史。

单个文件默认不超过 25 MiB，可用 `LOVIA_MAX_UPLOAD_MB` 调整。带扩展名的文件受内置白名单限制；
`LOVIA_UPLOAD_ALLOWED_EXT` 可覆盖白名单，使用逗号或空格分隔，`*` 表示不限。

通过文件面板上传的文件不会自动附加到下一条消息。`tmp/`、`node_modules/`、`venv/` 和
`__pycache__/` 等目录默认隐藏。删除聊天也不会删除 `uploads/` 中的文件，需要由应用或用户清理。

后台进程属于聊天而不是单次 Run：Run 结束不会终止它们；删除聊天或停止 Web 服务时会统一清理，
服务重启后也不会恢复。更完整的生命周期见[工作区](workspace.md#后台进程)。

## 常用 CLI 选项

除模型和搜索配置外，其余选项按“命令行参数 → 环境变量 → 默认值”解析。

| 命令行选项 | 环境变量 | 默认值 |
| --- | --- | --- |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--token` | `LOVIA_WEB_TOKEN` | 回环地址无需设置；其他地址自动生成 |
| `--db` | `LOVIA_DB` | `./.lovia/<agent>.db` |
| `--app MODULE:ATTR` | `LOVIA_APP` | 创建默认 Agent |
| `--skills-dir` | `LOVIA_SKILLS_DIR` | 若存在则使用 `./.agents/skills` |
| `--memory-dir` / `--no-memory` | `LOVIA_MEMORY_DIR` | `./.lovia/memory` |
| `--workspace`，`--readonly` / `--trusted` / `--no-workspace` | `LOVIA_WORKSPACE`、`LOVIA_WORKSPACE_MODE` | `.`（coding 模式） |
| `--instructions-file` | `LOVIA_INSTRUCTIONS_FILE` | 若存在则使用 `AGENTS.md` |
| `--max-retries` / `--max-turns` | `LOVIA_MAX_RETRIES` / `LOVIA_MAX_TURNS` | `4` / `50` |
| `--no-followups` | `LOVIA_FOLLOWUPS` | 追问建议默认开启 |
| `--no-subagents` | — | 后台子 Agent 默认开启 |
| `--check` | — | 检查模型配置并退出 |

完整列表以 `lovia web --help` 为准。

## 来自 Agent 的提问

默认 Agent 带有 [`ask_human`](built-in-tools.md#询问人工) Tool。等待中的问题保存在服务端，
所以刷新或重新连接不会丢失。默认 10 分钟无人回答时，调用会取消，模型收到 Tool 错误后继续；
这也避免定时任务和后台任务长期占用运行槽位。

自定义 Agent 需要把同一个 `HumanChannel` 同时交给 Tool 和 Web 应用：

```python
from lovia import Agent
from lovia.tools import HumanChannel, ask_human
from lovia.web import serve

channel = HumanChannel()
agent = Agent(name="bot", model="<model>", tools=[ask_human(channel)])
serve(agent, question_channel=channel, question_timeout=600)
```

## 追问建议

追问建议来自一次独立模型调用，不会写入 Transcript。关闭它可以避免这次额外调用；服务端使用
`--no-followups`，也可以用 `LOVIA_FOLLOWUP_MODEL` 指向更便宜的模型。通过 `create_app()`
托管自定义 Agent 时，该功能默认关闭，需要显式开启。独立端点配置见
[Web 服务端](web-server.md#追问建议)。

## 运行与连接的边界

Run 由服务端托管，刷新或关闭页面不会取消它；重新连接后会从服务端状态继续。显式停止 Run 时，
已经完成的 Turn 仍保留在 Session 中。

服务优雅关闭时会保留主 Run 的 Checkpoint，重启后可以恢复；审批和 SSE 订阅等进程内状态不会恢复。
Shell 后台进程和后台子 Agent 也不会随 Checkpoint 恢复。

Run 执行期间发送的纯文本会作为下一 Turn 的追加指令；如果当前 Run 恰好结束，则按顺序进入新的
Run。附件不会排队，需要等当前 Run 结束后再发送。

## 后台子 Agent

`lovia web` 默认启用[后台子 Agent](multi-agent.md#后台子-agent)，可用 `--no-subagents` 关闭。
默认子 Agent 保留 Skills、Todo、Tool 和 Workspace，但不加载 Scheduling、Memory 或 Subagents，
因此不会递归派生。

自定义 Agent 需要自行添加 `Subagents` Plugin。`create_app()` 会把使用默认执行方式的 Plugin
接入 Web 托管；可用 `create_app(..., wire_subagents=False)` 禁用。直接挂载
`build_api_router` 时，需要调用一次 `wire_subagents(app)`。

每个后台任务都有独立 Session。任务完成后，报告会送回父对话：父 Run 仍在执行时进入下一 Turn；
父对话空闲时，服务端会自动启动一次 Run 处理报告。浏览器无需保持在线。

后台任务不属于父 Run，因此停止父 Run 不会停止它。等待审批超过 `approval_timeout` 时会自动拒绝；
服务重启后，尚未完成的后台任务不会恢复。

## 延伸阅读

- [Web 服务端](web-server.md)：Python API、生命周期和定时任务
- [HTTP API](http-api.md)：构建自己的前端
- [工具审批](tools.md#工具审批)：审批流程及默认拒绝规则
- 示例：[`26_web_serve.py`](../../examples/26_web_serve.py)
