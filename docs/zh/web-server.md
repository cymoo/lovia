# Web 服务端

可选 Web 包为一个或多个 Agent 提供轻量 FastAPI 服务。独立进程使用 `serve()`；已有 ASGI
生命周期管理时使用 `create_app()`。

```bash
pip install "lovia[web]"
```

```python
from lovia import Agent
from lovia.web import serve

agent = Agent(name="assistant", model="<model>")
serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

## `serve()` 与 `create_app()`

`serve(agent_or_agents, *, host="127.0.0.1", port=8000, ...)` 创建应用并运行 uvicorn。
`log_level`、`ssl_certfile`、`workers` 等额外服务选项会透传。`create_app(...)` 只返回 ASGI
应用，不启动进程。

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `agent_or_agents` | 必填 | 一个 Agent 或 `{name: agent}` 映射 |
| `db_path` / `store` / `session` | `./.lovia/<agent>.db` | Transcript 和聊天元数据存储 |
| `max_turns` / `budget` / `retry` / `context_policy` | — | 应用于每个托管 Run 的设置 |
| `tracer` | `None` | 托管 Run 的 Span 记录器 |
| `generate_titles` / `title_model` | `True` / Agent 模型 | 在后台生成对话标题 |
| `approval_timeout` | `None` | N 秒后自动拒绝未解决的审批 |
| `max_background_runs`（仅 `create_app()`） | `8` | 并发托管 Run；达到上限后，新请求返回 429 |
| `ui` | `True` | 设为 `False` 时只提供 API |
| `cors_origins` | `None` | 允许的浏览器 Origin；不设置就不发送 CORS header |
| `token` / `auth` | `None` | 使用 Bearer token 保护业务 API，或传入自定义 FastAPI 依赖（见下文） |
| `title` / `empty_title` / `empty_description` | lovia 默认文案 | UI 文案和品牌 |
| `empty_examples` | `None` | 空白聊天页上的示例问题；点击后填入输入框，但不会自动发送 |

端点契约与 `ChatStore` 接口见 [HTTP API](http-api.md)。

## 认证

`serve()` 绑定回环地址时默认不要求凭据。绑定非回环地址时，如果既未传入 `token`
也未传入 `auth`，它会自动生成一个 token，并在启动时打印一次，同时给出可直接打开的
`/?token=...` UI 链接。因此，通过 `serve()` 启动时，API 不会未经认证就暴露在
非回环地址上。

```python
serve(agent, host="0.0.0.0", token="s3cret")        # 固定 token
serve(agent, host="0.0.0.0")                        # 自动生成并打印
```

token 会保护 `build_api_router` 注册的业务路由。`/healthz`、`/api/docs`、
`/api/openapi.json`、UI 页面和静态资源不包含会话或工作区数据，默认不要求认证。
客户端可以通过两种方式提交 token：

- **API 客户端**发送 `Authorization: Bearer <token>`。SSE 流由 `fetch` 读取，
  同样可以携带这个请求头。
- **内置 UI** 会把 token 保存到 cookie。token 可以从 `/?token=...` 链接自动读取，
  也可以在收到 401 后按提示输入。这样，`<img>` 预览和下载链接也会自动携带凭据。

如需会话认证、OAuth 或按用户区分身份，可以传入任意 FastAPI 依赖来替换内置检查；
保护范围仍是同一组路由：

```python
async def my_auth(request: Request) -> None:
    if not valid(request):
        raise HTTPException(status_code=401)

serve(agent, host="0.0.0.0", auth=my_auth)
```

`create_app()` 也接受这两个参数，但默认不会生成 token。自行管理应用时，请显式配置
认证依赖。

## 托管 Run 生命周期

流式 Run 是服务端持有的 Task。SSE 订阅者可以断开并重新连接，不会取消工作。

- **用户取消**：把已完成 Turn 写入 Session，移除悬空 Tool 调用并清理 Checkpoint。
- **服务端关闭**：协作式取消 Run，但保留 Checkpoint，以便部署后重新连接并恢复。
- **容量**：由 `max_background_runs` 限制；满载时新请求返回 HTTP 429。
- **阻塞式 `/api/chat`**：不受托管。前端应使用 `/api/chat/stream`。

正在运行的 Run、审批和 SSE Hub 都是进程内状态，因此只运行一个 Worker。SQLite 数据使用 WAL
并可跨重启保存，但这不会让内存中的托管状态自动支持多 Worker。

## 定时任务

Web 包持久化 Schedule，并支持三种触发方式：

| 触发器 | 值 |
| --- | --- |
| `at` | 一个 ISO-8601 时间戳或 Epoch 时间 |
| `every` | 秒数间隔 |
| `cron` | Cron 表达式；`lovia[web]` 已包含 `croniter` |

`Scheduling(store)` 提供需要审批的 `schedule_run` Tool。模型可以建议创建定时运行，
只有用户批准 Tool 调用后才会保存日程。`continue_session=True` 会把结果追加到同一聊天；
否则每次触发都创建新的 Session。每次触发至多投递一次；上一次运行尚未结束时，本次触发
直接跳过，不会排队积压。

## 安全检查

- 个人使用时保持 `host="127.0.0.1"`。非回环绑定会自动启用 token 验证；请妥善保管
  token，将其视同密码。
- 面向不可信用户时，应限制或关闭可写 Workspace。任何持有 token 的人都可以让 Agent
  修改文件或执行 Shell 命令。
- 设置 `approval_timeout`，避免无人处理的弹窗长期占用容量。
- 只使用一个 Worker，并备份 SQLite 数据库。
- 多用户部署还需要 TLS、按用户认证（`auth=`）和限流。共享 token 只适合单用户场景。

生产使用前请阅读完整的[生产部署](deployment.md)指南。

## 延伸阅读

- [Web UI](web-ui.md)：内置浏览器体验与 CLI
- [HTTP API](http-api.md)：端点、SSE 格式和 `ChatStore`
- [工具审批](tools.md#工具审批)：服务端审批流程
