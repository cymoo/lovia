# HTTP API

JSON + SSE API 与内置聊天页面彼此独立：只保留接口、不启用 UI，即可接入自己的前端或其他服务。
内置 UI 的所有功能都通过这些路由实现；服务启动后，可在 `/api/docs` 查看完整的交互式接口文档。

## 只提供 API

有两种集成深度。最简单的是直接关闭页面：

```python
from lovia.web import create_app

app = create_app(agent, ui=False)   # 没有 GET /，也没有 /static；只有 API
```

也可以把 router 挂到你自己的 FastAPI app 上，接入自己的 middleware、认证和生命周期：

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

`RouterDeps` 是普通 dataclass。`agents`、`store`、`approvals` 必填；运行设置
（`max_turns`、`budget`、`retry`、`tracer`、`approval_timeout`、`max_background_runs`、
标题选项）是字段，默认值和 `create_app` 一致。

## 认证

通过 `create_app(token=...)` 或 `serve(token=...)` 配置 token 后，
`build_api_router` 注册的业务路由都需要认证；`serve()` 绑定非回环地址且未指定认证方式时，
也会自动生成 token。
普通请求和 SSE 均应发送 `Authorization: Bearer <token>`。SSE 由 `fetch` 读取，
因此同样可以携带请求头。`GET /healthz` 始终开放。凭据缺失或错误时，服务端返回 `401`，
`detail` 中会包含 *server token* 字样，便于客户端区分服务端认证失败与模型 Provider
认证失败。直接挂载 `build_api_router` 时，需要自行添加依赖，例如
`lovia.web.auth.token_dependency(token)` 或其他 FastAPI 认证依赖。

`/api/docs` 和 `/api/openapi.json` 由 FastAPI 应用本身提供，不属于上述业务路由，默认保持
公开；其中只包含接口定义，不包含会话或工作区数据。如需限制访问，请在应用层另行处理。

## 端点

| Method & path | 用途 |
| --- | --- |
| `GET /healthz` | 存活检查 |
| `GET /api/info` | title、agents、默认 agent、版本、feature flags |
| `GET /api/agents` · `GET /api/agents/{name}` | agent 自省（instructions、工具、能力） |
| `POST /api/chat` | 执行一个**阻塞**轮次 → `{output, session_id, usage}` |
| `POST /api/chat/stream` | **SSE**：启动运行，或连接到 session 中正在运行的任务（可注入新消息） |
| `POST /api/chat/reconnect?session_id=` | **SSE**：刷新后重新连接，或恢复中断 checkpoint |
| `POST /api/chat/approve` | 处理未决审批：`{session_id, call_id, decision}` |
| `POST /api/chat/cancel?session_id=` | 停止正在运行的任务（保留已完成轮次） |
| `POST /api/chat/inject` / `uninject` | 为正在运行的任务排队 / 撤回[追加指令](cancellation.md#在运行中追加指令) |
| `GET /api/sessions?q=&limit=&offset=` | 列出 / 搜索聊天（置顶优先，可分页）；`DELETE` 清空全部 |
| `GET /api/runs` | 正在运行的服务端托管任务 |
| `GET /api/runs/history?session_id=&source=&since=&limit=&offset=` | 持久化的运行记录（结果、错误、时长、token 用量）；`since` 只保留在该时间戳之后结束的运行 |
| `GET /api/events` | **SSE**：进程级生命周期事件流——`run_started` / `run_finished`（携带运行记录状态）、`session_created` / `session_retitled`——UI 由轮询改为推送。不做重放：每次（重）连接先拉一次快照，之后信任事件流 |
| `GET` / `PATCH` / `DELETE /api/sessions/{id}` | transcript · 重命名/置顶 · 删除 |
| `GET /api/sessions/{id}/todos` | 当前 [Todo 列表](todo.md)，从 Transcript 重建 |
| `POST /api/sessions/{id}/rewind` | 从索引为 `user_turn` 的用户消息起删除后续内容，索引从 0 开始（用于编辑后重发或重新生成）；运行中返回 409，存储不支持 `rewind` 时返回 501 |
| `GET /api/sessions/{id}/export?format=md\|json\|txt` | 导出聊天 |
| `GET` / `POST /api/schedules`, `GET` / `PATCH` / `DELETE /api/schedules/{id}`, `POST .../run` | [定时运行](web-server.md#定时任务)：列出、创建、改时间/暂停、删除、立即触发 |
| `GET /api/schedules/{id}/runs` | 定时任务的触发历史（它的运行记录，从新到旧） |
| `GET /api/workspace` · `/files` · `/recent` · `/file` · `/raw` | 基于 agent [工作区](workspace.md)的只读文件面板 |
| `GET` / `PUT /api/memory?agent=` | 读取 / 替换 [Memory notes](memory.md#记忆如何写入)（`{content, used, budget}`） |

以下行为需要特别注意：

- 某个 stream 正在使用 session 时，`/api/chat` 返回 409。
- 对同一个运行中 session 再次发起 stream，会连接到现有运行，而不是另起一个运行或报错。
- Workspace 路由始终使用只读 session，不受 Agent 自身模式影响，并沿用 Agent 的
  `denied_paths`。接口还会过滤 `__pycache__`、`*.pyc`、`venv`、`node_modules` 等可重新
  生成的文件；dotfile 也不会显示，使 `/recent` 只关注用户文件。

## SSE 流

`POST /api/chat/stream`（以及 `/reconnect`）返回 `text/event-stream`，内容是
`event:` / `data:` 对，也就是 runner 的[类型化事件](streaming.md#事件清单)经过 JSON 编码后的形式：

| SSE event | Payload |
| --- | --- |
| `session` | `{session_id}`：新 stream 的第一帧 |
| `snapshot` | `{session_id, status, entries[]}`：重新连接的前奏，包含目前已完成轮次 |
| `text_delta` / `reasoning_delta` | `{delta}` |
| `output_discarded` | `{}`：清除当前轮次已渲染 delta |
| `message_completed` | `{message}`：一轮组装完成的 assistant 回复 |
| `user_injected` | `{content, turn}` |
| `tool_call` / `tool_result` | `{id, name, arguments}` / `{id, name, result, is_error}` |
| `todo` | `{call_id, todos: [...]}`：结构化 todo 更新 |
| `approval_required` | `{id, name, arguments}` → 通过 `POST /api/chat/approve` 回答 |
| `handoff` / `turn_started` / `context_compacted` | 转移和[压缩 notice](context.md) |
| `error` | `{type, message}`：工具范围错误，或 stream 随后结束时的终止错误 |
| `done` | `{output, usage}`：终止成功 |

重新连接不使用 Last-Event-Id。连接中断后，客户端重新 POST `/api/chat/reconnect`，会先收到
最新的 `snapshot`，随后重放当前轮次尚在进行的事件，包括仍待处理的 `approval_required`，
最后接入实时流。如果订阅队列溢出，服务端也会关闭连接，客户端按同样方式重连。以 `:` 开头
的注释行是保活信号，客户端应忽略。

## 内置浏览器客户端

`lovia/web/static/js/api.js` 是零依赖客户端，覆盖所有端点（`api.chat`、`api.streamChat`、
`api.reconnect`、`api.approve`、sessions、schedules、workspace、memory），并提供
`readSSE(response)`：一个遍历 `{event, data}` 对的 async generator。

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
置顶状态和可恢复 `active_run_id` 的 `ChatMeta` 表，以及 checkpointer 和 schedules 表。
`ChatStore.sqlite(path, wal=False)` 将这些数据放进同一个文件；`ChatStore.in_memory()` 适合
测试和演示；`ChatStore(session=..., meta_path=...)` 可以接入自定义 `Session` 后端，
同时保留聊天元数据功能。

## 注意事项

- **`build_api_router` 本身不包含认证或限流。** `create_app(token=...)` 和
  `serve(token=...)` 可以加上 token 验证，`serve()` 在非回环地址上还会自动生成 token
  （见[认证](#认证)）。单一共享 token 只适合单用户场景；多用户身份、权限和配额应由网关
  负责。`cors_origins` 默认为空，只有显式配置后才会发送 CORS 响应头。
- **SSE 响应由 POST 发起**，不是 `EventSource` 兼容的 GET。请像 `api.js` 一样用 `fetch` + reader；
  原生 `EventSource` 不能用。
- **`tool_result` 里的 `result` 是原始值**（JSON-safe 形态），和
  [`ToolCallCompleted`](streaming.md#工具与审批) 一样有双重性。需要结构化数据时读取 `result`，否则回退到字符串。
- **snapshot 按轮次，不按 token。** 重新连接时如果在句子中间，会从轮次缓冲区重放这个句子的 delta；
  渲染器要能接受已经画过的 delta 再出现一次（按轮次幂等渲染，或在 `snapshot` 时直接清空）。

## 延伸阅读

- [Web 服务端](web-server.md)：这些路由外面的服务端
- [流式输出](streaming.md)：同一套事件的进程内形式
- 示例：[`27_web_api.py`](../../examples/27_web_api.py)
