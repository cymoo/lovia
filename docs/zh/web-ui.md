# Web UI

可选的浏览器 UI 可以把任意 lovia Agent 变成本地聊天应用。它支持流式文本、Tool 调用记录、
对话历史、编辑后重发、重新生成、标题、审批、定时任务、记忆编辑、输入框旁的上下文用量
圆环（点击可查看 token / 缓存 / 模型详情），以及
只读的 Workspace 文件面板。

界面提供中文和英文，默认跟随浏览器语言，也可以在设置中手动切换。主题支持跟随系统、浅色
和深色三种模式；后台任务完成后是否发送桌面通知，也可以在设置中控制。所有前端资源都随
软件包提供，不依赖 CDN 或外部字体。

## 一条命令启动

```bash
pip install "lovia[web]"
lovia web
```

打开 `http://127.0.0.1:8000`。首次启动时，CLI 会询问缺失的模型配置、验证连接，并可保存到
`./.env`。

默认 Agent 包含 `Todo`、`./skills` 下的可选 Skills、`./.lovia/memory` 下的 Memory、时间和
HTTP Tool、Web 搜索（设置 `TAVILY_API_KEY` 后优先使用 Tavily，否则尝试可选的 DuckDuckGo）、
定时任务，以及根目录为当前目录的 coding 模式 Workspace。
如果存在 `AGENTS.md`，其内容会成为 Agent 的 instructions。

!!! danger "默认仅供本机使用；对外监听时自动启用 token 验证"

    默认绑定 `127.0.0.1`，无需凭据。绑定其他地址时必须使用 API token：可以通过
    `--token` 或 `LOVIA_WEB_TOKEN` 指定；如果没有指定，服务会自动生成并打印，同时
    给出可直接打开的 `/?token=...` 链接。UI 会保存这个 token，收到 401 时也会提示
    输入。持有 token 的客户端可以使用 Agent 的全部能力，包括文件编辑和 Shell 命令，
    因此应将它视同密码；离开本机使用时，建议同时启用 `--readonly`。多用户部署请参阅
    [生产部署](deployment.md)。

## 服务自己的 Agent

创建 `app.py`：

```python
from lovia import Agent

assistant = Agent(
    name="assistant",
    instructions="清晰回答，并在有助于提高准确性时使用工具。",
    model="<model>",
)
```

然后让 CLI 加载该对象：

```bash
lovia web --app app:assistant
```

`--app MODULE:ATTR` 可以指向一个 Agent 或 `{name: agent}` 映射。Python 部署和 ASGI 集成
详见 [Web 服务端](web-server.md)。

## 常用 CLI 选项

每个选项按以下顺序解析：命令行参数、环境变量、`./.env`（或 `--env-file`）、默认值。

| Flag | 环境变量 | 默认值 |
| --- | --- | --- |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--token` | `LOVIA_WEB_TOKEN` | 回环地址无需设置；绑定其他地址时自动生成并打印 |
| `--db` | `LOVIA_DB` | `./.lovia/<agent>.db` |
| `--model` | `LOVIA_MODEL` | 首次运行时询问 |
| `--app MODULE:ATTR` | `LOVIA_APP` | 创建默认 Agent |
| `--skills-dir` | `LOVIA_SKILLS_DIR` | 存在时使用 `./skills` |
| `--memory-dir` / `--no-memory` | `LOVIA_MEMORY_DIR` | `./.lovia/memory` |
| `--workspace` / `--readonly` / `--no-workspace` | `LOVIA_WORKSPACE` | `.`，coding 模式 |
| `--instructions-file` | `LOVIA_INSTRUCTIONS_FILE` | 存在时使用 `AGENTS.md` |
| `--max-retries` / `--max-turns` | `LOVIA_MAX_RETRIES` / `LOVIA_MAX_TURNS` | `4` / `50` |
| `--env-file` | — | 存在时使用 `./.env` |

完整选项见 `lovia web --help`，其中还包括 TLS、Provider 超时、上下文窗口和代理配置。

## 浏览器断开后会发生什么

Run 由服务端持有，SSE 连接只是订阅者。关闭或刷新浏览器不会停止工作。重新打开对话时，
客户端会先收到已完成 Turn 的快照、当前 Turn 的回放，再接上实时事件。只有点击停止按钮才会
显式取消 Run，并把已经完成的 Turn 保留到 Session。

正在运行的会话会在侧栏显示脉动圆点，也可以直接从侧栏停止。UI 通过服务端的生命周期
事件流（`/api/events`，不可用时回退为轮询）感知后台状态：未打开的会话在后台运行结束
时，页面会弹出提示；如果标签页处于后台，浏览器标签页标题中还会显示未读数；页面完全
关闭期间结束的运行，会在下次打开页面时补发提示。
定时任务会保留完整的运行历史，并在面板中逐次显示 ✓ 或 ✕（含时长与 token 用量）；将
鼠标停在失败标记上即可查看错误信息。

## 延伸阅读

- [Web 服务端](web-server.md)：Python API、生命周期和定时任务
- [HTTP API](http-api.md)：构建自己的前端
- [工具审批](tools.md#工具审批)：审批弹窗如何取得决定
- 示例：[`26_web_serve.py`](../../examples/26_web_serve.py)
