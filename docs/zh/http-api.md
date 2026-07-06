# HTTP API

JSON + SSE API 和内置聊天页面是解耦的：保留端点，去掉 UI，就可以把自己的前端（或另一个服务）
接上来。内置 UI 做的所有事都通过这些路由；任意运行中的服务都可以在 `/api/docs` 查看完整交互式 schema。

## 不带 UI 服务 API

两种深度。直接关闭页面：

```python
from lovia.web import create_app

app = create_app(agent, ui=False)   # 没有 GET /，也没有 /static；只有 API
```

或者把 router 挂到你自己的 FastAPI app 上（你的 middleware、认证、生命周期）：

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

## 端点

| Method & path | 用途 |
| --- | --- |
| `GET /healthz` | 存活检查 |
| `GET /api/info` | title、agents、默认 agent、版本、feature flags |
| `GET /api/agents` · `GET /api/agents/{name}` | agent 自省（instructions、工具、能力） |
| `POST /api/chat` | 一个**阻塞** turn → `{output, session_id, usage}` |
| `POST /api/chat/stream` | **SSE**：启动运行，或 attach 到 session 中正在运行的任务（注入新消息） |
| `POST /api/chat/reconnect?session_id=` | **SSE**：刷新后重新 attach，或恢复中断 checkpoint |
| `POST /api/chat/approve` | 处理未决审批：`{session_id, call_id, decision}` |
| `POST /api/chat/cancel?session_id=` | 停止正在运行的任务（保留已完成 turn） |
| `POST /api/chat/inject` / `uninject` | 为正在运行的任务排队 / 撤回[追加指令](reliability.md#运行中追加指令) |
| `GET /api/sessions?q=&limit=` | 列出 / 搜索聊天（置顶优先）；`DELETE` 清空全部 |
| `GET /api/runs` | 正在运行的服务端托管任务 |
| `GET` / `PATCH` / `DELETE /api/sessions/{id}` | transcript · 重命名/置顶 · 删除 |
| `GET /api/sessions/{id}/todos` | 当前 [todo list](plugins.md#todo)，从 transcript 重建 |
| `GET /api/sessions/{id}/export?format=md\|json\|txt` | 导出聊天 |
| `GET` / `POST /api/schedules`, `GET` / `PATCH` / `DELETE /api/schedules/{id}`, `POST .../run` | [定时运行](web.md#定时任务)：列出、创建、改时间/暂停、删除、立即触发 |
| `GET /api/workspace` · `/files` · `/recent` · `/file` · `/raw` | 基于 agent [工作区](workspace.md)的只读文件面板 |
| `GET` / `PUT /api/memory?agent=` | 读取 / 替换 [Memory notes](memory.md#记忆如何写入)（`{content, used, budget}`） |

需要知道的语义：当某个 stream 拥有 session 时，`/api/chat` 返回 409；在正在运行的 session 上启动第二个
stream 会 attach，而不是报错；workspace 路由不管 agent 自己是什么模式，都会使用强制 readonly session
（继承 agent 的 `denied_paths`），并隐藏可再生的环境垃圾（`__pycache__`、`*.pyc`、`venv`、
`node_modules`——点文件本来就隐藏），让 `/recent` 始终围绕用户的真实文件。

## SSE 流

`POST /api/chat/stream`（以及 `/reconnect`）返回 `text/event-stream`，内容是
`event:` / `data:` 对，也就是 runner 的[类型化事件](streaming.md#事件清单)经过 JSON 编码后的形式：

| SSE event | Payload |
| --- | --- |
| `session` | `{session_id}`：新 stream 的第一帧 |
| `snapshot` | `{session_id, status, entries[]}`：重新 attach 的前奏，包含目前已完成 turn |
| `text_delta` / `reasoning_delta` | `{delta}` |
| `output_discarded` | `{}`：清除当前 turn 已渲染 delta |
| `message_completed` | `{message}`：一个组装完成的 assistant turn |
| `user_injected` | `{content, turn}` |
| `tool_call` / `tool_result` | `{id, name, arguments}` / `{id, name, result, is_error}` |
| `todo` | `{call_id, todos: [...]}`：结构化 todo 更新 |
| `approval_required` | `{id, name, arguments}` → 通过 `POST /api/chat/approve` 回答 |
| `handoff` / `turn_started` / `context_compacted` | 转移和[压缩 notice](context.md) |
| `error` | `{type, message}`：工具范围错误，或 stream 随后结束时的终止错误 |
| `done` | `{output, usage}`：终止成功 |

reconnect 契约刻意简单：没有 Last-Event-Id 账本。客户端丢连接后（或订阅队列溢出，服务端会关闭慢消费者）
只需重新 POST `/api/chat/reconnect`，就会收到新的权威 `snapshot`、当前 turn 在途事件回放
（包括仍未处理的 `approval_required`），然后接上实时流。以 `:` 开头的 comment line 是 keep-alive，
跳过即可。

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

你可以直接 import 它，也可以把它当成任何语言客户端的参考实现。它刻意保持很小。

## ChatStore

`ChatStore` 是 API 背后的存储包：一个 `Session`（transcript）、metadata 表（`ChatMeta` 行：
标题、时间戳、置顶、可恢复的 `active_run_id`）、checkpointer 和 schedules 表。
`ChatStore.sqlite(path, wal=False)` 把一切放进一个文件；`ChatStore.in_memory()` 用于测试和 demo；
`ChatStore(session=..., meta_path=...)` 可以包住自定义 `Session` 后端，同时保留 metadata 功能。

## 容易踩的点

- **没有 auth，没有 rate limit**：这是组件，不是产品边界。请挂在自己的 gateway 后面；`cors_origins`
  默认不设置（无 CORS），直到你明确开启。
- **SSE 响应由 POST 发起**，不是 `EventSource` 兼容的 GET。请像 `api.js` 一样用 `fetch` + reader；
  原生 `EventSource` 不能用。
- **`tool_result` 里的 `result` 是原始值**（JSON-safe 形态），和
  [`ToolCallCompleted`](streaming.md#工具与审批) 一样有双重性。需要结构时渲染 `result`，否则回退到字符串。
- **snapshot 按 turn，不按 token。** 重新 attach 时如果在句子中间，会从 turn buffer 重放这个句子的 delta；
  渲染器要能接受已经画过的 delta 再出现一次（按 turn 幂等渲染，或在 `snapshot` 时直接清空）。

## 延伸阅读

- [Web UI 与服务端](web.md)：这些路由外面的服务端
- [流式输出](streaming.md)：同一套事件的进程内形式
- 示例：[`27_web_api.py`](../../examples/27_web_api.py)
