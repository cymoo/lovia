# Web UI

浏览器 UI 可以把任意 lovia Agent 变成本地聊天应用，支持流式输出、Tool 调用记录、
对话历史、编辑后重发、重新生成、对话标题、审批、定时任务、记忆编辑、图片与文件上传，
以及只读 Workspace 文件面板。
所有前端资源都随软件包提供，不依赖 CDN 或外部字体。

## 一条命令启动

```bash
pip install "lovia[web]"
lovia web
```

打开 `http://127.0.0.1:8000`。首次启动时，如果模型配置不完整，CLI 会提示补全并验证连接，
还可保存到 `.lovia/config.env`（仅本人可读、已加入 .gitignore），下次无需重输。

CLI 创建的默认 Agent 会启用 `Todo`、`./.lovia/memory` 中的 Memory、时间与 HTTP Tool、
Web 搜索、定时任务，以及以当前目录为根的 coding 模式 Workspace。如果 `./skills` 目录存在，
还会加载其中的 Skills。设置 `TAVILY_API_KEY` 后，Web 搜索使用 Tavily；否则尝试使用可选的
DuckDuckGo 后端。如果当前目录存在 `AGENTS.md`，其内容会作为 Agent 的 instructions。

!!! danger "默认仅供本机使用；对外监听时自动启用 token 验证"

    默认绑定 `127.0.0.1`，无需凭据。绑定其他地址时必须使用 API token，可通过 `--token`
    或 `LOVIA_WEB_TOKEN` 指定；如果未指定，服务会自动生成并打印，同时给出可直接打开的
    `/?token=...` 链接。任何持有 token 的客户端都可以使用 Agent 的全部能力，包括编辑文件
    和执行 Shell 命令，因此应将 token 视同密码。允许其他设备访问时，建议同时启用
    `--readonly`。多用户部署请参阅[生产部署](deployment.md)。

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

## 图片与文件

附件（Composer 的 **+**、拖拽或粘贴）会上传到 Workspace 的 `uploads/` 目录，并以
workspace 路径在消息中被引用，因此无论用哪个模型，Agent 都能用文件工具打开它们；
`--no-workspace` 会隐藏该按钮。

图片还会在模型支持视觉时**内联**发送：

- **主模型支持视觉。** 官方 `api.openai.com` / `api.anthropic.com` 域名默认按多模态处理。
  其他端点（Qwen-VL / DashScope、vLLM 部署，或可能后接纯文本模型的兼容网关）需用
  `LOVIA_VISION=1` 显式声明。
- **主模型为纯文本。** 设置 `LOVIA_VISION_MODEL=<vendor>:<model>`（例如
  `openai:qwen3.7-plus`）会注册一个 `see_image` 工具：主模型把"看这张图"委托给该视觉模型，
  拿回一段文字答复，图片字节不会进入主对话历史。`vendor:` 前缀决定 API 方言（规则同
  `LOVIA_MODEL`），端点与密钥默认复用该前缀对应的 `OPENAI_*` / `ANTHROPIC_*`；视觉模型不在
  同一端点时，用 `LOVIA_VISION_BASE_URL` / `LOVIA_VISION_API_KEY` 覆盖。

上传大小上限 25 MiB（`LOVIA_MAX_UPLOAD_MB`），文件类型限于内置的常见图片、文档、数据与
代码扩展名白名单（`LOVIA_UPLOAD_ALLOWED_EXT`，逗号/空格分隔，`*` 表示不限）。

## 常用 CLI 选项

每个选项按以下顺序解析：命令行参数、环境变量、`.lovia/config.env`、`./.env`
（或 `--env-file` 指定的文件）、默认值。

| 命令行选项 | 环境变量 | 默认值 |
| --- | --- | --- |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--token` | `LOVIA_WEB_TOKEN` | 回环地址无需设置；绑定其他地址时自动生成并打印 |
| `--db` | `LOVIA_DB` | `./.lovia/<agent>.db` |
| `--model` | `LOVIA_MODEL` | 首次运行时询问 |
| `--app MODULE:ATTR` | `LOVIA_APP` | 创建默认 Agent |
| `--skills-dir` | `LOVIA_SKILLS_DIR` | 若存在则使用 `./skills` |
| `--memory-dir` / `--no-memory` | `LOVIA_MEMORY_DIR` | `./.lovia/memory` |
| `--workspace`，`--readonly` / `--trusted` / `--no-workspace` | `LOVIA_WORKSPACE`、`LOVIA_WORKSPACE_MODE` | `.`（coding 模式） |
| `--instructions-file` | `LOVIA_INSTRUCTIONS_FILE` | 若存在则使用 `AGENTS.md` |
| `--max-retries` / `--max-turns` | `LOVIA_MAX_RETRIES` / `LOVIA_MAX_TURNS` | `4` / `50` |
| `--env-file` | — | `.lovia/config.env`，然后 `./.env` |

完整选项见 `lovia web --help`，按 model / agent / server / advanced 四组呈现：
上下文窗口在 model 组，输出 token 上限、Provider 超时与重试、代理与日志级别在 advanced 组。

## 关闭或刷新页面

Run 由服务端托管，关闭或刷新页面不会中断运行。重新打开对话后，UI 会恢复当前进度并继续
显示实时输出。只有点击停止按钮才会取消运行；已经完成的 Turn 仍会保留在 Session 中。
正在运行的会话会在侧栏显示状态，也可以直接从侧栏停止。

## 延伸阅读

- [Web 服务端](web-server.md)：Python API、生命周期和定时任务
- [HTTP API](http-api.md)：构建自己的前端
- [工具审批](tools.md#工具审批)：审批弹窗的处理流程
- 示例：[`26_web_serve.py`](../../examples/26_web_serve.py)
