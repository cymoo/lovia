# Web UI

可选的浏览器 UI 可以把任意 lovia Agent 变成本地聊天应用。它提供流式文本与 Tool 活动、
对话历史、标题、审批、定时任务、记忆编辑器和只读 Workspace 文件面板。浏览器资源全部随
软件包提供，不依赖 CDN 或外部字体。

## 一条命令启动

```bash
pip install "lovia[web]"
lovia web
```

打开 `http://127.0.0.1:8000`。首次启动时，CLI 会询问缺失的模型配置、验证连接，并可保存到
`./.env`。

默认 Agent 包含 `Todo`、`./skills` 下的可选 Skills、`./.lovia/memory` 下的 Memory、时间和
HTTP Tool、可选 DuckDuckGo 搜索、定时任务，以及根目录为当前目录的 coding 模式 Workspace。
如果存在 `AGENTS.md`，其内容会成为 Agent 的 instructions。

!!! danger "默认仅供本机使用；不提供内置认证"

    个人使用时请保持默认的 `127.0.0.1` 绑定。暴露 UI 前应加入认证和限流，并限制或关闭
    可写 Workspace。详见[生产部署](deployment.md)。

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

## 延伸阅读

- [Web 服务端](web-server.md)：Python API、生命周期和定时任务
- [HTTP API](http-api.md)：构建自己的前端
- [工具审批](tools.md#工具审批)：审批弹窗如何取得决定
- 示例：[`26_web_serve.py`](../../examples/26_web_serve.py)
