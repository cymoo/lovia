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
| `max_background_runs` | `8` | 并发托管 Run；超额启动返回 429 |
| `ui` | `True` | 设为 `False` 时只提供 API |
| `cors_origins` | `None` | 允许的浏览器 Origin；不设置就不发送 CORS header |
| `token` / `auth` | `None` | `/api/*` 的 Bearer token 门禁，或自定义 FastAPI 依赖（见下文） |
| `title` / `empty_title` / `empty_description` | lovia 默认文案 | UI 文案和品牌 |
| `empty_examples` | `None` | 空白聊天页上可点击的示例 prompt（点击填入输入框） |

端点契约与 `ChatStore` 接口见 [HTTP API](http-api.md)。

## 认证

回环地址绑定无需任何凭据。除此之外 `serve()` 默认安全：绑定非回环地址时，
若既没传 `token` 也没传 `auth`，会自动生成一个 token 并打印一次——附带可直接
打开的 `/?token=...` UI 链接——API 永远不会在无认证的情况下暴露。

```python
serve(agent, host="0.0.0.0", token="s3cret")        # 固定 token
serve(agent, host="0.0.0.0")                        # 自动生成并打印
```

一个 token，两条通道，守住所有 `/api/*` 路由（`/healthz` 对探针保持开放；
UI 页面与静态资源本身不含数据，保持公开）：

- **API 客户端**发送 `Authorization: Bearer <token>`——SSE 也一样，因为流
  是用 `fetch` 消费的。
- **内置 UI** 把 token 存入 cookie（从 `/?token=...` 链接自动采集，或在
  401 时弹框输入），因此 `<img>` 预览和下载链接也能带上凭据。

需要会话、OAuth 或按用户识别身份时，用任意 FastAPI 依赖替换内置检查——
守卫的仍是同一批路由：

```python
async def my_auth(request: Request) -> None:
    if not valid(request):
        raise HTTPException(status_code=401)

serve(agent, host="0.0.0.0", auth=my_auth)
```

`create_app()` 接受同样的两个参数，但默认保持中立——不会替你生成 token；
自己掌管应用时请显式接线认证。

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

`Scheduling(store)` 提供带审批门禁的 `schedule_run` Tool。模型可以提出未来 Run，但只有用户
批准 Tool 调用后才会真正创建。`continue_session=True` 把结果追加到同一聊天，否则每次触发
都会创建新 Session。投递采用 at-most-once 并合并触发：上一次仍在运行时跳过本次触发。

## 安全检查

- 个人使用保持 `host="127.0.0.1"`；非回环绑定会自动加上 token 门禁，但此后
  一切都系于这个 token——请像对待密码一样对待它。
- 面向不可信用户时限制或关闭可写 Workspace：持有 token 的任何人都能让
  agent 改文件、跑 shell。
- 设置 `approval_timeout`，避免无人处理的弹窗长期占用容量。
- 只使用一个 Worker，并备份 SQLite 数据库。
- 真正的多用户暴露还需要 TLS、按用户认证（`auth=`）与限流——共享 token
  只是单用户级别的安全。

生产使用前请阅读完整的[生产部署](deployment.md)指南。

## 延伸阅读

- [Web UI](web-ui.md)：内置浏览器体验与 CLI
- [HTTP API](http-api.md)：端点、SSE 格式和 `ChatStore`
- [工具审批](tools.md#工具审批)：服务端审批流程
