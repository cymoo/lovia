# Web 服务端

Web 包基于 FastAPI，为一个或多个 Agent 提供 HTTP 服务。独立启动时使用 `serve()`；
集成到现有 ASGI 应用时使用 `create_app()`。

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

`serve(agent_or_agents, *, host="127.0.0.1", port=8000, ...)` 创建应用并交给 uvicorn
运行，`log_level`、`ssl_certfile`、`workers` 等选项会原样传给 uvicorn。
`create_app(...)` 只返回 ASGI 应用，不启动服务进程。

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `agent_or_agents` | 必填 | 一个 Agent 或 `{name: agent}` 映射 |
| `db_path` / `store` / `session` | `./.lovia/<agent>.db` | Transcript 和聊天元数据存储 |
| `max_turns` / `budget` / `retry` / `context_policy` | — | 应用于每个托管 Run 的设置 |
| `tracer` | `None` | 托管 Run 的 Span 记录器 |
| `generate_titles` / `title_model` | `True` / Agent 模型 | 在后台生成对话标题 |
| `approval_timeout` | `None` | 超过指定秒数后自动拒绝未处理的审批 |
| `max_background_runs`（仅 `create_app()`） | `8` | 并发托管 Run；达到上限后，新请求返回 429 |
| `ui` | `True` | 设为 `False` 时只提供 API |
| `cors_origins` | `None` | 允许跨域访问的浏览器 Origin；不设置时不发送 CORS 响应头 |
| `token` / `auth` | `None` | 使用 Bearer token 保护业务 API，或传入自定义 FastAPI 依赖（见下文） |
| `title` / `empty_title` / `empty_description` | lovia 默认文案 | UI 文案和品牌 |
| `empty_examples` | `None` | 空白聊天页上的示例问题；点击后填入输入框，但不会自动发送 |

`serve()` 固定使用 `max_background_runs=8`；如需调整，请通过 `create_app()` 创建应用，
再交给 ASGI 服务器运行。

端点契约与 `ChatStore` 接口见 [HTTP API](http-api.md)。

## 认证

`serve()` 绑定回环地址时默认不要求凭据。绑定非回环地址时，如果既未传入 `token`，
也未传入 `auth`，服务会自动生成 token，并在启动时打印一次，同时给出可直接打开的
`/?token=...` UI 链接。因此，通过 `serve()` 启动的业务 API 不会在非回环地址上匿名开放。

```python
serve(agent, host="0.0.0.0", token="s3cret")        # 固定 token
serve(agent, host="0.0.0.0")                        # 自动生成并打印
```

token 会保护 `build_api_router` 注册的业务路由。`/healthz`、`/api/docs`、
`/api/openapi.json`、UI 页面和静态资源默认不要求认证。客户端可以通过以下方式提交 token：

- **普通 API 请求和聊天 SSE**：发送 `Authorization: Bearer <token>`。聊天流由
  `fetch` 读取，可以携带请求头。
- **内置 UI**：将 token 保存到 cookie。`/api/events` 使用 `EventSource`，无法自定义
  请求头，因此会通过 cookie 认证；`<img>` 预览和下载链接也使用同一 cookie。UI 可从
  `/?token=...` 链接读取 token，也会在收到 401 后提示输入。

如需基于会话的认证、OAuth 或用户级身份，可以传入任意 FastAPI 依赖来替换内置检查；
保护范围仍是同一组路由：

```python
async def my_auth(request: Request) -> None:
    if not valid(request):
        raise HTTPException(status_code=401)

serve(agent, host="0.0.0.0", auth=my_auth)
```

`create_app()` 也接受 `token` 和 `auth`，但默认不启用认证。自行管理应用时，请显式传入
其中一个。

## 托管 Run 生命周期

流式 Run 由服务端后台任务托管。SSE 连接断开后，Run 仍会继续执行，客户端可以稍后重连。

- **用户取消**：把已完成 Turn 写入 Session，移除悬空 Tool 调用并清理 Checkpoint。
- **服务端关闭**：协作式取消 Run，但保留 Checkpoint，以便部署后重新连接并恢复。
- **容量**：由 `max_background_runs` 限制；满载时新请求返回 HTTP 429。
- **阻塞式 `/api/chat`**：不受托管。前端应使用 `/api/chat/stream`。

正在运行的 Run、审批状态和 SSE 订阅都保存在进程内，因此只能使用一个 Worker。SQLite 数据
可以跨重启保留，但持久化存储无法让这些进程内状态支持多 Worker。

## 定时任务

Web 包会持久化定时任务，支持三种触发方式：

| 触发器 | 值 |
| --- | --- |
| `at` | ISO-8601 时间戳或 Unix 时间戳 |
| `every` | 秒数间隔 |
| `cron` | Cron 表达式；`lovia[web]` 已包含 `croniter` |

`Scheduling(store)` 提供需要审批的 `schedule_run` 工具。模型可以建议创建定时任务，
但只有用户批准工具调用后才会保存。`continue_session=True` 会把结果追加到同一对话；
如果该对话正有 Run 在执行，定时指令会直接注入当前 Run。设置
`continue_session=False` 后，每次触发都会创建新的 Session；如果上一次定时 Run 尚未结束，
本次触发会直接跳过，不会排队。服务停机期间错过多个触发时刻时，恢复后只补发一次。

重复任务可以设置自然语言停止条件 `until`，例如“每分钟检查日志，直到出现 ready”。
每次运行完成任务后，模型都会收到检查该条件的指令；如果条件满足，它会调用
`cancel_schedule` 停用任务。使用 `until` 时还必须设置以下至少一项硬性上限，以免模型
没有识别出条件已经满足：

- `max_fires`：最多触发指定次数；
- `expires_at`：到期后不再触发。

插件还提供无需审批的 `list_schedules` 和 `cancel_schedule`。取消操作只会停用任务，
之后仍可在面板中恢复或删除。停止条件通常由无人值守的定时运行自行处理，而这类运行中的
审批请求会被自动拒绝，因此 `cancel_schedule` 不能依赖审批。

## 安全检查

- 个人使用时保持 `host="127.0.0.1"`。`serve()` 绑定非回环地址时会自动启用 token 验证；
  请妥善保管 token，将其视同密码。
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
