# Web UI 与服务端

用户能和 agent 对话，它才算真正可用。可选 web 层是一个小型 FastAPI 应用：聊天 UI、SSE
流式输出、带标题的 session、审批、定时任务、记忆编辑器、只读文件面板。它可以服务任意 lovia
agent；如果你想自带前端，[HTTP API](http-api.md) 也能独立使用。内置页面完全自包含：
渲染器随包提供，使用系统字体，不请求 CDN 或字体源，所以内网、离线、防火墙后都能正常加载。

```bash
pip install "lovia[web]"
```

```python
from lovia.web import serve

serve(agent, host="127.0.0.1", port=8000, db_path="lovia.db")
```

也可以完全不写代码：

```bash
python -m lovia.web
```

## `serve()` 和 `create_app()`

`serve(agent_or_agents, *, host="127.0.0.1", port=8000, ...)` 会构建 app 并运行 uvicorn
（额外 kwargs 如 `log_level`、`ssl_certfile`、`workers` 会透传）。`create_app(...)` 返回
ASGI app，供你自己的进程管理器使用。重要选项：

| 选项 | 默认值 | 作用 |
| --- | --- | --- |
| `agent_or_agents` | 必填 | 一个 agent，或 `{name: agent}` dict，用来服务多个 agent |
| `db_path` / `store` / `session` | cwd 下的 `<agent>.db` | transcript + metadata 存在哪里（[`ChatStore`](http-api.md#chatstore)） |
| `max_turns` / `budget` / `retry` / `context_policy` / `tracer` | — | 应用于每个被服务运行的设置 |
| `generate_titles` / `title_model` | `True` / 服务 agent 的模型 | 后台 LLM 聊天标题；标题生成前显示第一条用户消息，手动重命名始终优先 |
| `approval_timeout` | `None` | N 秒后自动拒绝未处理审批 |
| `max_background_runs` | `8`（`create_app`） | 并发 supervised runs；超额启动返回 HTTP 429 |
| `ui` | `True` | `False` = 只提供 API（没有 `GET /` 或 `/static`） |
| `cors_origins` | `None` | 不设置 = 不发 CORS header（跨域浏览器请求会被拒） |
| `title` / `empty_title` / `empty_description` | `"lovia"` / `"Wake up, Neo."` / … | 品牌文案 |

## 零配置 CLI

`python -m lovia.web` 会创建一个默认 agent 并启动服务。具体组合是：模型来自环境变量；
存在 `./skills` 时加载 `Skills("./skills")`；启用 `Todo()` checklist；启用 `Scheduling`
（agent 可以提出未来运行，审批门禁）；在 `./.lovia/memory` 下启用后台整理的 `Memory`；
工具包含 `now` + `http_fetch`（安装了 `ddg` 后端时再加 `web_search`）；当前目录上启用
**trusted 工作区**；把今天日期作为 instruction 片段；如果存在 `AGENTS.md`，就用它作为 instructions。

每个选项按命令行参数 → 环境变量 → 默认值的顺序读取。安装了 `python-dotenv` 时，会自动加载
`./.env`（也可以传 `--env-file`）。模型凭证使用 provider 自己的变量（见[Provider](providers.md)）。

| Flag | Env | 默认 |
| --- | --- | --- |
| `--host` / `--port` | `LOVIA_HOST` / `LOVIA_PORT` | `127.0.0.1` / `8000` |
| `--db` | `LOVIA_DB` | cwd 下的 `<agent>.db` |
| `--model` | `LOVIA_MODEL` → `OPENAI_DEFAULT_MODEL` → `ANTHROPIC_DEFAULT_MODEL` | 必填 |
| `--skills-dir`（可重复） | `LOVIA_SKILLS_DIR` | `./skills`（存在时） |
| `--memory-dir` / `--no-memory` | `LOVIA_MEMORY_DIR` | `./.lovia/memory`（开启） |
| `--workspace` / `--workspace-mode` / `--no-workspace` | `LOVIA_WORKSPACE` / `LOVIA_WORKSPACE_MODE` | `.` / `trusted`（开启） |
| `--instructions` / `--instructions-file` | `LOVIA_INSTRUCTIONS_FILE` | `AGENTS.md`，否则通用 instructions |
| `--app MODULE:ATTR` | `LOVIA_APP` | 构建默认 agent |
| `--title` / `--log-level` | `LOVIA_TITLE` / `LOVIA_LOG_LEVEL` | `lovia` / `info` |
| `--max-retries` | `LOVIA_MAX_RETRIES` | agent 应对策略（3 次重试）；`0` 关闭 |
| `--provider-timeout` | `LOVIA_PROVIDER_TIMEOUT` | `60`s |
| `--max-tokens` / `--context-window` | `LOVIA_MAX_TOKENS` / `LOVIA_CONTEXT_WINDOW` | provider 默认 / 向 provider 询问 |
| `--max-turns` | `LOVIA_MAX_TURNS` | `50` |
| `--trust-env` | `LOVIA_PROVIDER_TRUST_ENV` | 关闭（开启后遵守 `HTTP(S)_PROXY`） |
| `--env-file`（可重复）/ `--version` | — | 存在时加载 `./.env` / 打印版本 |

`--app mymodule:assistant` 会服务你自己的 agent（此时默认 agent 相关选项会被忽略，并给出 warning）。
`--provider-timeout` 和 `--trust-env` 作用于 provider 本身，所以对 `--app` agent 也生效；
`--max-retries` / `--max-turns` 应用于每个被服务的运行；`--max-tokens` /
`--context-window` 只配置默认 agent。

如果要访问使用内网 TLS 的模型端点，`web` extra 会带上 `truststore`，因此自动信任 OS 证书库；
`LOVIA_HTTP_CA_BUNDLE` / `LOVIA_HTTP_INSECURE` 仍是[手动覆盖](providers.md#网络超时代理tls)。

## 运行不会随浏览器关闭而停止

被服务的运行是 **supervised** 的：运行是服务端拥有的 task，SSE 连接只是订阅者。笔记本中途合盖，
运行仍会继续；重新打开聊天会重新 attach。客户端先收到已完成 turn 的权威 snapshot，再收到当前
turn 在途事件的回放（包括仍在等待的 approval），然后接上实时流。

相关生命周期：

- **停止**（`POST /api/chat/cancel`，UI 的 stop 按钮）会取消运行，并把已完成 turn finalize 进 session，
  作为[调用方决定的 partial](sessions-and-checkpoints.md#契约)（悬空工具调用会丢掉，checkpoint 清理）。
  对话保留用户看到的内容，且不会重复计数。
- **服务端关闭**会协作式取消 supervised runs，但**保留 checkpoint**，所以重新部署后可以在 reconnect
  时恢复中断运行（`POST /api/chat/reconnect`）；后台[记忆整理](memory.md)会在有界等待内 drain。
- **容量**：达到 `max_background_runs` 时，新启动返回 429，scheduler 会推迟触发。

## 定时任务

服务端在持久化 `schedules` 表上运行一个小 scheduler（可通过 API 创建，也可由 agent 自己创建）：

- **触发器**：`at`（一次性，ISO-8601 或 epoch）、`every`（间隔秒数）、`cron`
  （通过 `croniter`，随 `lovia[web]` 安装）。
- **`schedule_run` 工具**（默认 agent，或你自己的 agent 上使用 `Scheduling(store)`）允许**模型**
  提出后续运行，比如“周五提醒我”。它带[审批](human-in-the-loop.md)门禁，所以没有点击确认就不会被安排。
  `continue_session=True` 会把结果落在同一聊天中；否则每次触发都会打开一个新 session。
- **至多投递一次，并合并重叠触发**：上一次触发仍在运行时，新触发会跳过（不排队）；暂停的 schedule 保持暂停
  （用 `PATCH` 修改 `active`，用 `POST .../run` 手动触发）。

## 容易踩的点

- **没有认证。** 服务端信任每个请求。请绑定 loopback（默认），或在暴露前加自己的认证代理。CLI 在你把
  非 loopback 地址和可写工作区组合时会明确警告，因为这相当于“以你的用户身份远程执行代码”。
- **默认工作区模式是在 cwd 上 `trusted`**。这适合项目目录里的个人助手，不适合共享环境。先用
  `--workspace-mode readonly` 或 `--no-workspace`，之后再逐步放开。
- **supervised 状态是进程内的。** 正在运行的任务、approvals、SSE hubs 都在内存里：请运行一个进程
  （`workers=1`）。SQLite stores 使用 `wal`，所以**数据**能存活；多 worker 部署需要粘性路由，
  通常不值得做。
- **`/api/chat`（阻塞）不是 supervised**：不能 reconnect，也不能 attach。UI 应该使用
  `/api/chat/stream`；阻塞路由是给脚本用的。

## 延伸阅读

- [HTTP API](http-api.md)：每个端点、SSE 传输格式、自带前端之外的接入方式
- [记忆](memory.md#记忆如何写入)：侧边栏编辑器的后端
- 示例：[`26_web_serve.py`](../../examples/26_web_serve.py)
