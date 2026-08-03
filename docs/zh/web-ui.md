# Web UI

浏览器 UI 可以把任意 lovia Agent 变成本地聊天应用。它支持流式输出、工具调用记录、
对话历史、编辑最后一条用户消息后重发、重新生成、对话标题、追问建议、审批、定时任务、
记忆编辑、图片与文件上传，以及 Workspace 文件浏览和预览。
所有前端资源都随软件包分发，不依赖 CDN 或外部字体。

## 一条命令启动

```bash
pip install "lovia[web]"
lovia web
```

打开 `http://127.0.0.1:8000`。首次启动（无论有无终端，Docker 亦然）浏览器会显示
引导页——在设置里填好第一个模型即可开始对话，这是唯一的配置入口；文件也可以手写。
配置保存到用户级的 `~/.lovia/config.json`，此后从任意目录启动都能读取；仅对当前项目
生效的 `./.lovia/config.json` 存在时整体优先。在支持 Unix 权限的平台上，配置文件仅允许
当前用户读写；CLI 还会在 `.lovia/` 目录中创建 `.gitignore`，避免密钥和聊天数据被意外
提交。

之后修改配置走「设置 → 模型」；运行 `lovia web --check` 会显示当前配置并探测端点，
但不会启动服务。

CLI 创建的默认 Agent 会启用 `Todo`、`./.lovia/memory` 中的 Memory、时间与 HTTP 工具、
Web 搜索、定时任务，以及以当前目录为根、采用 `coding` 模式的 Workspace。如果
`./.agents/skills` 目录存在，还会自动加载其中的 Skills。这个目录遵循跨 Agent 的通用约定，
可以与 Claude Code、Codex 等工具共用。0.9.13 起，旧的 `./skills` 不再自动加载；可以将其
移到新位置，或显式传入 `--skills-dir skills`。在配置中填入 Tavily API Key 后，Web 搜索
使用 Tavily；否则尝试使用可选的 DuckDuckGo 后端。如果当前目录存在 `AGENTS.md`，其内容会
作为 Agent 的 instructions。

!!! danger "默认仅供本机使用；对外监听时自动启用 token 验证"

    默认绑定 `127.0.0.1`，无需凭据。绑定其他地址时必须使用 API token，可通过 `--token`
    或 `LOVIA_WEB_TOKEN` 指定；如果未指定，服务会自动生成并打印，同时给出可直接打开的
    `/?token=...` 链接。任何持有 token 的客户端都可以使用 Agent 的全部能力，包括编辑文件
    和执行 Shell 命令，因此应将 token 视同密码。允许其他设备访问时，建议同时启用
    `--readonly`。多用户部署请参阅[生产部署](deployment.md)。

## 模型配置与切换

「设置 → 模型」管理着 `config.json` 中的模型档案：显示名称、API 格式（OpenAI 兼容 /
Anthropic）、Base URL、API Key、模型 ID，以及可选的上下文窗口与视觉能力声明。表单里的
「测试连接」会真实探测端点（可达性、认证、`/models` 列表、上下文窗口回读），「获取列表」
把端点公布的模型放进候选——未列出的模型照样可以手动填写。密钥只写不读：界面只显示
`已设置 · sk-…1234`，完整值绝不回传浏览器。

输入框右侧的模型标签显示当前对话使用的模型，点击即可在档案间切换。切换立即对新消息
生效（包括继续旧对话、定时任务与子代理），进行中的回复不受影响；所有打开的标签页通过
事件流自动同步。「角色指派」把**视觉理解**（对话模型不支持图片时的 `see_image` 委托）与
**辅助任务**（标题与追问建议，可指向更便宜的模型）绑定到具体档案。

「设置 → 搜索」管理 `web_search` 后端（自动 / Tavily / DuckDuckGo / 关闭）与 Tavily
API Key。所有修改经服务端整体校验后原子写入 `config.json` 并立即生效，无需重启。

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

然后让 CLI 加载这个对象：

```bash
lovia web --app app:assistant
```

`--app MODULE:ATTR` 可以指向一个 Agent，也可以指向 `{name: agent}` 映射。Python 部署和
ASGI 集成详见 [Web 服务端](web-server.md)。

## 回复的渲染格式

聊天界面使用 GitHub 风格 Markdown 渲染回复，支持表格、代码高亮、Mermaid 图表和
内嵌图片。`lovia web` 创建的默认 Agent 已在系统提示词中说明这些能力；通过 `--app`、
`serve()` 或 `create_app()` 加载的自定义 Agent 不会自动获得这段说明。

如果希望模型主动使用这些格式，可将 `SURFACE_NOTE` 加入 `instructions`：

```python
from lovia.web import SURFACE_NOTE

assistant = Agent(
    name="assistant",
    instructions="清晰回答。\n\n" + SURFACE_NOTE,
    model="<model>",
)
```

## 图片与文件

通过输入框旁的 **+** 按钮、拖拽或粘贴添加附件后，文件会上传到 Workspace 的
`uploads/` 目录，并以相对于 Workspace 根目录的路径附加到消息中。因此，即使模型不支持
直接接收附件，Agent 仍可通过 Workspace 工具按路径访问。使用 `--no-workspace` 时，
附件按钮会隐藏。

JPG、PNG、GIF 和 WebP 图片还可在模型支持视觉时**内联**发送：

- **主模型支持视觉。** 官方 `api.openai.com` / `api.anthropic.com` 端点默认视为支持
  视觉。其他端点（如 Qwen-VL / DashScope 或部署在 vLLM 上的视觉模型）需要设置
  `LOVIA_VISION=1`；如果兼容网关后接的是纯文本模型，则不要启用。
- **主模型为纯文本。** 设置 `LOVIA_VISION_MODEL=<vendor>:<model>`（例如
  `openai:qwen3.7-plus`）会注册 `see_image` 工具。主模型通过该工具把图片问题交给视觉
  模型，再接收文字答复；图片原始数据不会写入主模型的对话历史。`vendor:` 前缀的解析规则
  与 `LOVIA_MODEL` 相同，端点和密钥默认读取对应的 `OPENAI_*` 或 `ANTHROPIC_*` 变量。
  如果视觉模型位于另一端点，可用 `LOVIA_VISION_BASE_URL` 和 `LOVIA_VISION_API_KEY`
  单独配置。

每个文件的上传上限为 25 MiB（`LOVIA_MAX_UPLOAD_MB`）。带扩展名的文件必须符合内置的
常见图片、文档、数据和代码类型白名单；无扩展名的文件默认允许。可通过
`LOVIA_UPLOAD_ALLOWED_EXT` 自定义白名单，多个扩展名用逗号或空格分隔，`*` 表示不限。

## 文件面板

文件面板提供“最近”和“浏览”两种视图。“最近”按修改时间排列，并优先列出当前对话中由
文件工具写入或编辑的文件。面板可以预览图片、SVG、PDF、Markdown、HTML、CSV/TSV、
代码和普通文本；HTML 会在隔离的沙箱中渲染，无法预览的二进制文件仍可下载。

从文件面板上传时，文件只会保存到 Workspace，不会自动随消息发送。打开文件后点击
“添加为对话附件”，可将其加入下一条消息；面板还支持复制路径和下载。它不会编辑或删除
已有文件。`tmp/`、`node_modules/`、`venv/` 和 `__pycache__/` 等临时目录或环境目录不会
出现在面板中，以免挤占“最近”列表。

文件列表上方还有一条**后台进程**区块：当前对话用 `shell(background=true)` 启动的进程
（dev server、watcher 等）会列在这里，显示运行状态与退出码，并附终止按钮——不必让模型
代劳即可停掉一个 server。这些进程随聊天存亡：run 结束不影响它们，删除聊天或停止
`lovia web`（Ctrl+C）时统一收割；服务重启后它们不会自动恢复。

## 常用 CLI 选项

模型连接（模型名、Base URL、API Key、上下文窗口）、多模型档案、角色指派与 Web 搜索
只存在于 `config.json` 中——没有对应的命令行参数或环境变量——在网页「设置」中管理；
项目级 `./.lovia/config.json` 存在时整体优先于用户级。其余
选项按 命令行参数 > 环境变量 > 默认值 取值。`--check` 会打印当前配置并探测端点，但
不会启动服务。

| 命令行选项 | 环境变量 | 默认值 |
| --- | --- | --- |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--token` | `LOVIA_WEB_TOKEN` | 回环地址无需设置；绑定其他地址时自动生成并打印 |
| `--db` | `LOVIA_DB` | `./.lovia/<agent>.db` |
| `--app MODULE:ATTR` | `LOVIA_APP` | 创建默认 Agent |
| `--skills-dir` | `LOVIA_SKILLS_DIR` | 若存在则使用 `./.agents/skills` |
| `--memory-dir` / `--no-memory` | `LOVIA_MEMORY_DIR` | `./.lovia/memory` |
| `--workspace`，`--readonly` / `--trusted` / `--no-workspace` | `LOVIA_WORKSPACE`、`LOVIA_WORKSPACE_MODE` | `.`（coding 模式） |
| `--instructions-file` | `LOVIA_INSTRUCTIONS_FILE` | 若存在则使用 `AGENTS.md` |
| `--max-retries` / `--max-turns` | `LOVIA_MAX_RETRIES` / `LOVIA_MAX_TURNS` | `4` / `50` |
| `--no-followups` | `LOVIA_FOLLOWUPS` | 默认开启（可在 `config.json` 中指派辅助模型） |
| `--no-subagents` | — | 后台子 Agent 默认开启 |
| `--check` | — | 显示当前配置并探测端点后退出 |

完整选项见 `lovia web --help`。帮助信息分为 model（`--check`）、agent、
server 和 advanced 四组；输出 token 上限、Provider 超时与重试、代理和日志级别位于
advanced 组。

## 来自 Agent 的提问

默认 Agent 带有 [`ask_human`](built-in-tools.md#询问人工) 工具：当模型需要一个只有
你能做的决定时，运行会挂起，聊天里出现交互问题卡——问题本身、至多四个选项按钮（各带
一行说明），以及始终可用的自由文本输入逃生门。多选问题渲染为复选框，所选 label 按
每行一个发送。问题挂在服务端而非页面里，因此刷新或重连后卡片会重建、仍可回答；无人
回答的问题 10 分钟后自动取消——模型收到工具错误并继续——定时或后台运行永远不会
被永久卡住。

自定义 Agent 时，把同一个 channel 同时接到工具和应用上：

```python
from lovia import Agent
from lovia.tools import HumanChannel, ask_human
from lovia.web import serve

channel = HumanChannel()
agent = Agent(name="bot", model="<model>", tools=[ask_human(channel)])
serve(agent, question_channel=channel, question_timeout=600)
```

## 追问建议

Run 成功产出回答后，UI 会在答案下方显示几个可直接点击发送的追问。建议由一次独立的
模型调用生成，不会写入当前对话；Run 中途停止、执行报错或没有合适建议时均不显示。
生成结果会按会话缓存在当前浏览器中。刷新页面或切换到其他会话再返回时，只要该功能仍然
开启且对话末尾没有变化，原有建议就会恢复，无需再次调用模型；继续发送消息后，旧建议
不再显示。

在 **设置 →「回答结束后建议追问」** 中关闭后，当前浏览器不会再发起生成请求，因此不会
产生额外模型开销，也不需要重启服务。服务端可用 `--no-followups` 整体关闭，或通过
`LOVIA_FOLLOWUP_MODEL` 改用更便宜的模型。使用 `create_app()` 托管自定义 Agent 时，
该功能默认关闭，需要显式开启。如果追问模型位于独立端点，可另设
`LOVIA_FOLLOWUP_BASE_URL` 和 `LOVIA_FOLLOWUP_API_KEY`。详见
[Web 服务端](web-server.md#追问建议)。

## 运行中继续发送消息

Run 尚未结束时仍可发送纯文本消息。UI 会将它显示为排队状态，并作为下一轮追加指令交给
当前 Run；在进入下一轮处理前，可以点击消息上的取消按钮将其撤回。如果消息恰好赶上
当前 Run 结束，UI 会按原顺序将它转入后续 Run。

附件不能在 Run 执行期间排队，需要等当前 Run 结束后再发送。

## 后台子 Agent

`lovia web` 默认启用[后台子 Agent](multi-agent.md#后台子-agent)，可用
`--no-subagents` 关闭。默认助手可以把无需查看父对话也能理解的完整任务说明交给
后台助手；后台助手保留 Skills、Todo、工具和 Workspace，但不加载 Scheduling、
Memory 或 Subagents，因此不会继续递归派生。

使用自定义 Agent 时，需要自行添加 `Subagents` 插件。`create_app()` 会自动将仍采用
默认执行和投递方式的插件切换为 Web 托管模式；可通过
`create_app(..., wire_subagents=False)` 禁用。若应用直接挂载
`build_api_router`，则需调用一次 `wire_subagents(app)`。

**每个后台任务都是独立会话**，由**聊天顶栏的任务按钮**呈现——仅当当前对话
有后台任务时出现，运行中脉冲、待审批时琥珀色。点开弹层列出本对话的任务
（`t1`、`t2`……带实时耗时或终态），任务会话不进入左侧全局对话列表。点开任务
后与普通对话一样支持流式显示和断线重连，也可以停止或删除任务，
并直接批准或拒绝工具调用。若审批在 `approval_timeout` 内无人处理，服务端会自动
拒绝；`lovia web` 的默认超时时间为 10 分钟，避免任务一直占用并发名额。
每个任务都会保留状态和 Token 用量等运行记录，来源标记为
`subagent:<session>`。

**报告会自动送回原对话。** 如果父对话仍有 Run 在执行，报告会在下一轮作为用户侧
消息注入；如果父对话已经空闲，服务端会以报告为输入自动启动一次 Run，并保存模型的
后续回复，不要求浏览器保持在线。父会话暂时达到并发上限时，服务端会间隔重试；
多次尝试仍失败才会记录警告。

后台任务独立于父 Run，因此停止父对话不会同时停止它。可以在 **任务** 分组中单独
停止；父 Run 尚未结束时，模型也可以调用 `cancel_subagent`。服务重启后，进行中的
后台任务不会自动恢复。

整条生命周期在服务端日志里清晰可读：任务启动/结束、报告是注入还是新起 Run
投递、Run 卡在审批上等待。`lovia web` 还会给受管 Run 内产生的每条日志打上来源
标记——`[user]`、`[schedule:<id>]`、子任务 Run 的 `[subagent:<session>]`、
投递报告 Run 的 `[subagent-report:<task>]`——并行的后台工作与主对话一眼可分。自定义部署在日志格式中加入 `%(run_source)s` 并给 handler
挂上 `lovia.web.supervisor.run_source_log_filter()` 即可获得同样的标记。

## 关闭或刷新页面

Run 由服务端托管，关闭或刷新页面不会中断运行。重新打开对话后，UI 会恢复当前进度并继续
显示实时输出。流式连接意外中断时，UI 会自动尝试重新连接并补齐缺失内容；多次失败后会
显示手动“重新连接”按钮。

只有点击停止按钮才会取消运行；已经完成的 Turn 仍会保留在 Session 中。正在运行的会话会
在侧栏显示状态，也可以直接从侧栏停止。

## 延伸阅读

- [Web 服务端](web-server.md)：Python API、生命周期和定时任务
- [HTTP API](http-api.md)：构建自己的前端
- [工具审批](tools.md#工具审批)：审批弹窗的处理流程
- 示例：[`26_web_serve.py`](../../examples/26_web_serve.py)
