# HTTP API

JSON + SSE API 与内置聊天页面相互独立。可以关闭 UI，只保留接口，再接入自己的前端或其他
服务。内置 UI 也使用同一组接口；服务启动后，可在 `/api/docs` 查看交互式 API 文档。

## 只提供 API

有两种接入方式。最简单的是关闭内置页面：

```python
from lovia.web import create_app

app = create_app(agent, ui=False)   # 没有 GET /，也没有 /static；只有 API
```

也可以将 router 挂载到自己的 FastAPI 应用，由现有应用统一管理中间件、认证和生命周期：

```python
from fastapi import FastAPI

from lovia.web import ChatStore, RouterDeps, build_api_router
from lovia.web.approvals import ApprovalRegistry

deps = RouterDeps(
    agents={"bot": agent},
    store=ChatStore.in_memory(),
    approvals=ApprovalRegistry(),
)
app = FastAPI()
app.include_router(build_api_router(deps))
```

`RouterDeps` 是普通 dataclass，`agents`、`store` 和 `approvals` 为必填字段。
`max_turns`、`budget`、`retry`、`tracer`、`approval_timeout`、`max_background_runs`
及标题设置均有与 `create_app()` 相同的默认值。

## 认证

通过 `create_app(token=...)` 或 `serve(token=...)` 配置 token 后，
`build_api_router` 注册的业务路由都需要认证。`serve()` 绑定非回环地址且未指定认证方式时，
还会自动生成 token。

普通请求以及 `POST /api/chat/stream`、`POST /api/chat/reconnect` 应发送
`Authorization: Bearer <token>`。`GET /api/events` 使用 `EventSource`，无法自定义请求头，
内置 UI 因此通过 `lovia_token` cookie 认证。`GET /healthz` 始终开放。凭据缺失或错误时，
服务端返回 `401`；`detail` 中会包含 *server token*，便于客户端区分服务端认证失败和模型
Provider 认证失败。

直接挂载 `build_api_router` 时，需要自行添加认证依赖，例如
`lovia.web.auth.token_dependency(token)` 或其他 FastAPI 依赖。

`/api/docs` 和 `/api/openapi.json` 由 FastAPI 应用本身提供，不属于上述业务路由，默认保持
公开；其中只包含接口定义，不包含会话或工作区数据。如需限制访问，请在应用层另行处理。

## 端点

| 方法与路径 | 用途 |
| --- | --- |
| `GET /healthz` | 存活检查 |
| `GET /api/info` | 标题、Agent 列表、默认 Agent、版本和功能开关 |
| `GET /api/agents` · `GET /api/agents/{name}` | 查看 Agent 的 instructions、工具和能力 |
| `POST /api/chat` | 执行一次**阻塞式** Run → `{output, session_id, usage}` |
| `POST /api/chat/stream` | **SSE**：启动 Run，或连接到 Session 中正在执行的 Run（新消息会作为追加指令注入） |
| `POST /api/chat/reconnect?session_id=` | **SSE**：重新连接，或从 Checkpoint 恢复中断的 Run |
| `POST /api/chat/approve` | 处理未决审批：`{session_id, call_id, decision}` |
| `POST /api/chat/cancel?session_id=` | 取消正在执行的 Run（保留已完成的 Turn） |
| `POST /api/chat/inject` / `uninject` | 为正在执行的 Run 排队或撤回[追加指令](cancellation.md#在运行中追加指令) |
| `GET /api/sessions?q=&limit=&offset=` | 列出或搜索对话（置顶优先，支持分页）；`DELETE` 清空全部对话 |
| `GET /api/runs` | 正在运行的服务端托管任务 |
| `GET /api/runs/history?session_id=&source=&since=&limit=&offset=` | 查询持久化的运行记录，包括结果、错误、时长和 token 用量；`since` 按结束时间过滤 |
| `GET /api/events` | **SSE**：订阅进程级生命周期事件（不重放历史事件） |
| `GET` / `PATCH` / `DELETE /api/sessions/{id}` | 查看 transcript；重命名或置顶；删除 |
| `GET /api/sessions/{id}/todos` | 当前 [Todo 列表](todo.md)，从 Transcript 重建 |
| `POST /api/sessions/{id}/rewind` | 从索引为 `user_turn` 的用户消息起删除后续内容，索引从 0 开始；运行中返回 409，不支持 `rewind` 时返回 501 |
| `GET /api/sessions/{id}/export?format=md\|json\|txt` | 导出聊天 |
| `GET` / `POST /api/schedules`, `GET` / `PATCH` / `DELETE /api/schedules/{id}`, `POST .../run` | [定时运行](web-server.md#定时任务)：列出、创建、改时间/暂停、删除、立即触发 |
| `GET /api/schedules/{id}/runs` | 按时间倒序列出定时任务的运行记录 |
| `GET /api/workspace` · `/files` · `/recent` · `/file` · `/raw` | 读取 Agent [工作区](workspace.md)中的文件 |
| `GET` / `PUT /api/memory?agent=` | 读取 / 替换 [Memory notes](memory.md#记忆如何写入)（`{content, used, budget}`） |

### 生命周期事件

`GET /api/events` 使用 GET + `EventSource`，推送 `run_started`、`run_finished`、
`session_created` 和 `session_retitled`。事件流不重放历史；客户端每次连接或重连时，
应先通过 `/api/sessions` 和 `/api/runs` 获取一次当前状态，再处理后续事件。订阅者处理过慢时，
服务端会关闭连接，客户端仍按上述流程恢复。如需补查断线期间已经结束的 Run，可调用
`/api/runs/history` 并传入 `since`。

### 其他行为

- 某个聊天流正在占用 Session 时，`/api/chat` 返回 409。
- 同一 Session 中已有 Run 正在执行时，再次发起聊天流会连接到现有 Run，而不是另起 Run
  或报错。
- Workspace 路由始终以只读模式访问工作区，不受 Agent 自身模式影响，并沿用 Agent 的
  `denied_paths`。接口还会过滤 `__pycache__`、`*.pyc`、`venv`、`node_modules` 等可重新
  生成的文件；隐藏文件也不会显示，使 `/recent` 只列出用户文件。

## 聊天 SSE 流

`POST /api/chat/stream` 和 `/api/chat/reconnect` 返回 `text/event-stream`。每条消息由
`event:` 和 `data:` 组成，对应 Runner 的[类型化事件](streaming.md#事件清单)；`data:`
使用 JSON 编码。

| SSE 事件 | 数据 |
| --- | --- |
| `session` | `{session_id}`：新聊天流的第一条事件 |
| `snapshot` | `{session_id, status, entries[]}`：重连时的当前状态，包含已完成的 Turn |
| `text_delta` / `reasoning_delta` | `{delta}` |
| `output_discarded` | `{}`：清除当前 Turn 已显示的增量内容 |
| `message_completed` | `{message}`：完整的模型回复 |
| `user_injected` | `{content, turn}` |
| `tool_call` / `tool_result` | `{id, name, arguments}` / `{id, name, result, is_error}` |
| `todo` | `{call_id, todos: [...]}`：结构化 todo 更新 |
| `approval_required` | `{id, name, arguments}` → 通过 `POST /api/chat/approve` 回答 |
| `handoff` / `turn_started` / `context_compacted` | Handoff、Turn 开始和[上下文压缩](context.md) |
| `error` | `{type, message}`：Tool 错误，或聊天流终止前的运行错误 |
| `done` | `{output, usage}`：Run 成功结束 |

聊天流不使用 Last-Event-Id。连接中断后，客户端重新 POST `/api/chat/reconnect`，会依次收到
最新的 `snapshot`、当前 Turn 的事件回放和后续实时事件；尚未处理的 `approval_required`
也会重放。如果客户端处理过慢导致连接关闭，恢复方式相同。以 `:` 开头的注释行是保活信号，
应直接忽略。

## 内置浏览器客户端

`lovia/web/static/js/api.js` 是零依赖客户端，封装了聊天、Session、定时任务、Workspace 和
Memory 等接口，并提供 `readSSE(response)`，用于异步遍历 `{event, data}`。

```js
import { api, readSSE } from "./api.js";

const res = await api.streamChat({ message: "hello" });
for await (const { event, data } of readSSE(res)) {
  if (event === "text_delta") render(data.delta);
}
```

可以直接导入这个模块，也可以将其作为其他语言客户端的参考实现。代码量很小，便于移植。

## ChatStore

`ChatStore` 组合了 API 所需的几类存储：保存 transcript 的 `Session`、保存标题、时间戳、
置顶状态和可恢复 `active_run_id` 的 `ChatMeta` 表，以及 checkpointer、定时任务表和运行记录表。
`ChatStore.sqlite(path, wal=False)` 将这些数据放进同一个文件；`ChatStore.in_memory()` 适合
测试和演示；`ChatStore(session=..., meta_path=...)` 可以接入自定义 `Session` 后端，
同时保留聊天元数据功能。

## 注意事项

- **`build_api_router` 本身不包含认证或限流。** `create_app(token=...)` 和
  `serve(token=...)` 可以加上 token 验证，`serve()` 在非回环地址上还会自动生成 token
  （见[认证](#认证)）。单一共享 token 只适合单用户场景；多用户身份、权限和配额应由网关
  负责。`cors_origins` 默认为空，只有显式配置后才会发送 CORS 响应头。
- **聊天 SSE 由 POST 发起。** `/api/chat/stream` 和 `/api/chat/reconnect` 需要使用
  `fetch` + reader，不能使用原生 `EventSource`；`GET /api/events` 则专门供
  `EventSource` 使用。
- **`tool_result` 里的 `result` 是原始值**（JSON-safe 形态），和
  [`ToolCallCompleted`](streaming.md#工具与审批) 一样有双重性。需要结构化数据时读取
  `result`，否则回退到字符串。
- **`snapshot` 按 Turn 记录，不按 token 记录。** 如果在回复中途重连，服务端会重新发送
  当前 Turn 已产生的 delta。客户端应按 Turn 做幂等渲染，或在收到 `snapshot` 后清空当前
  Turn 的临时内容。

## 延伸阅读

- [Web 服务端](web-server.md)：承载这些路由的服务端配置
- [流式输出](streaming.md)：同一套事件的进程内形式
- 示例：[`27_web_api.py`](../../examples/27_web_api.py)
