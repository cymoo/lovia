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
| `title` / `empty_title` / `empty_description` | lovia 默认文案 | UI 文案和品牌 |

端点契约与 `ChatStore` 接口见 [HTTP API](http-api.md)。

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

- 除非应用位于带认证的反向代理后，否则保持 `host="127.0.0.1"`。
- 面向不可信用户时限制或关闭可写 Workspace。
- 设置 `approval_timeout`，避免无人处理的弹窗长期占用容量。
- 只使用一个 Worker，并备份 SQLite 数据库。
- 暴露到网络前增加请求级认证、授权和限流。

生产使用前请阅读完整的[生产部署](deployment.md)指南。

## 延伸阅读

- [Web UI](web-ui.md)：内置浏览器体验与 CLI
- [HTTP API](http-api.md)：端点、SSE 格式和 `ChatStore`
- [工具审批](tools.md#工具审批)：服务端审批流程
