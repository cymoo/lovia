# Web 服务端

Web 包基于 FastAPI。`create_app()` 构建 ASGI 应用，所有服务选项都在这里配置；
`serve()` 负责用 uvicorn 运行它。

```bash
pip install "lovia[web]"
```

```python
from lovia import Agent
from lovia.web import create_app, serve

agent = Agent(name="assistant", model="<model>")
serve(create_app(agent, db_path="lovia.db"), host="127.0.0.1", port=8000)
```

## `create_app()` 与 `serve()`

`serve(target, *, host="127.0.0.1", port=8000, **uvicorn_kwargs)` 运行由 `create_app()`
构建的应用；直接传入 Agent（或 `{name: agent}` 映射）时，则按默认选项构建应用，
`serve(agent)` 是最快的启动方式。`log_level`、`ssl_certfile`、`workers`、`root_path`
（[部署在代理后面时](#部署在路径前缀下)）等选项会原样传给 uvicorn。改用其他 ASGI 服务器时，
不必经过 `serve()`。

`create_app(agent_or_agents, ...)` 的选项：

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `agent_or_agents` | 必填 | 一个 Agent 或 `{name: agent}` 映射 |
| `db_path` / `store` / `session` | `./.lovia/<agent>.db` | Transcript 和聊天元数据存储 |
| `max_turns` / `budget` / `retry` / `context_policy` | — | 应用于每个托管 Run 的设置 |
| `tracer` | `None` | 托管 Run 的 Span 记录器 |
| `generate_titles` / `title_model` | `True` / Agent 模型 | 在后台生成对话标题 |
| `followups` / `followup_model` | `False` / Agent 模型 | 回答结束后生成追问建议（见下文） |
| `approval_timeout` | `None` | 超过指定秒数后自动拒绝未处理的审批 |
| `max_background_runs` | `8` | 并发托管 Run；达到上限后，新请求返回 429 |
| `ui` | `True` | 设为 `False` 时只提供 API；传入 `ChatUI` 可定制内置页面（见下文） |
| `cors_origins` | `None` | 允许跨域访问的浏览器 Origin；不设置时不发送 CORS 响应头 |
| `token` / `auth` | `None` | 使用 Bearer token 保护业务 API，或传入自定义 FastAPI 依赖（见下文） |
| `title` | `"lovia"` | 页面标题，也是 API 报告的标题 |

`ChatUI` 的选项（用于 `ui=ChatUI(...)`）：

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `empty_title` / `empty_description` | lovia 默认文案 | 空白聊天页的文案；说明可以是若干短句组成的列表 |
| `empty_examples` | `()` | 空白聊天页上的示例问题；点击后填入输入框，但不会自动发送 |
| `login_url` | `None` | 使用 `auth=` 时未登录用户的跳转地址（见[认证](#认证)） |
| `templates` | `None` | 自定义模板所在的目录（见[定制页面](#定制页面)） |

```python
from lovia.web import ChatUI, create_app

app = create_app(
    agent,
    title="Acme",
    ui=ChatUI(empty_title="问问 Acme", empty_examples=["总结今天的工单"]),
)
```

Transcript、聊天元数据和 Run 检查点共用同一个 SQLite 文件，并以 **WAL 模式**打开，
使界面的读取不必排在检查点写入之后。已有的数据库会在首次打开时完成迁移。
`<name>.db-wal` 和 `<name>.db-shm` 两个伴随文件只在有连接打开时存在，最后一个连接
关闭时会把 WAL 折回主库 —— 因此从运行中的服务器复制需要三个文件都带上，而已停止的
服务器仍然只留下一个文件。若文件系统不支持共享内存（部分网络挂载），SQLite 会拒绝
WAL 并记录一条警告，同时保留原有的 journal 模式；也可以直接传
`store=ChatStore.sqlite(path, wal=False)` 显式关闭。

端点契约与 `ChatStore` 接口见 [HTTP API](http-api.md)。

## 定制页面

`ChatUI(templates="ui")` 指向一个存放自定义 Jinja 模板的目录，其中的 `index.html` 会替换
内置页面。继承内置页面，只填写需要的 block 即可：

```html+jinja
{# ui/index.html #}
{% extends "lovia/index.html" %}

{% block head %}
<link rel="stylesheet" href="{{ url_for('brand', path='theme.css').path }}">
{% endblock %}

{% block sidebar_footer %}
<a class="signout" href="/logout">退出登录</a>
{% endblock %}
```

| Block | 渲染位置 |
| --- | --- |
| `head` | `<head>` 末尾：样式表、meta 标签 |
| `sidebar_footer` | 侧栏底部：用户菜单、退出登录链接 |
| `body_end` | 应用脚本之后：你自己的脚本 |

自己的静态文件可以挂载到应用上提供，并用 `url_for(...).path` 引用，这样部署在
[路径前缀下](#部署在路径前缀下)时链接仍然有效：

```python
from fastapi.staticfiles import StaticFiles

app = create_app(agent, ui=ChatUI(templates="ui"))
app.mount("/brand", StaticFiles(directory="brand"), name="brand")
```

更换主题时，覆盖内置 `styles.css` 开头的设计变量即可（`--bg`、`--surface`、`--ink`、
`--accent` 等；深色主题在 `[data-theme="dark"]` 下另有一套）。CSS 类名、元素 id 和
JavaScript 模块都属于内部实现，可能随版本变化。前端没有插件 API，`api.js` 是唯一有文档的
客户端。

## 追问建议

内置建议器会在 Run 成功产出回答后单独调用一次模型；这次请求不会写入主对话的
Transcript，也不会延迟原回答。
建议器只会把最近一组用户问题和模型回答发给追问模型，过长内容会先截断。

```python
create_app(agent, followups=True, followup_model="<small-model>")
```

`create_app()` 默认关闭该功能，因为每次生成建议都可能增加一次模型调用。
`followup_model` 可改用比 Agent 主模型更便宜的模型；不设置时沿用 Agent 的模型。
`lovia web` CLI 默认开启，可通过 `--no-followups` 或 `LOVIA_FOLLOWUPS=0` 关闭，
也可通过 `LOVIA_FOLLOWUP_MODEL` 指定其他模型。该模型位于独立端点时，再设置
`LOVIA_FOLLOWUP_BASE_URL` 和 `LOVIA_FOLLOWUP_API_KEY`。

如果建议来自 FAQ、向量库或自定义提示词，可以把异步函数直接传给 `followups`，
替换内置建议器：

```python
from lovia.web import FollowupRequest, create_app, generate_followups

async def pricing_only(request: FollowupRequest) -> list[str]:
    return await generate_followups(
        request, model="<model>", instructions="只提关于价格的问题。"
    )

create_app(agent, followups=pricing_only)
```

自定义建议器会收到 `FollowupRequest`，其中包含 `session_id`、Agent 的注册名称，以及由
Transcript 转换而来的聊天消息列表 `messages`。返回字符串序列即可；返回 `[]` 时不显示
建议。建议器抛出异常时，服务端会记录日志并返回空列表，不影响正常对话。

## 认证

`serve()` 绑定回环地址时默认不要求凭据。绑定非回环地址时，直接传入的 Agent 会自动获得
一个生成的 token，启动时打印一次，同时给出可直接打开的 `/?token=...` UI 链接；
而既未设置 `token` 也未设置 `auth` 的应用会被拒绝运行。因此，通过 `serve()` 启动的业务
API 不会在非回环地址上匿名开放。

```python
serve(create_app(agent, token="s3cret"), host="0.0.0.0")   # 固定 token
serve(agent, host="0.0.0.0")                                # 自动生成并打印
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

serve(create_app(agent, auth=my_auth), host="0.0.0.0")
```

它保护的范围和 token 完全相同，由此带来三点影响：

- **内置 UI 需要 cookie。** `EventSource`、`<img>` 预览和下载链接都无法携带自定义请求头，
  只读取 `Authorization` 的依赖会把它们挡在外面，因此还要接受你的会话 cookie。
- **自己添加的路由需要自己保护。** 挂载时加上 `dependencies=[Depends(my_auth)]`。
  UI 页面和 `/api/docs` 仍然公开；如需保护整个站点，请使用中间件或反向代理。
- **跨域前端使用 cookie 认证时**，需要允许携带凭据的 CORS，而 `cors_origins` 不会开启这一项
  （见下文）。

跨域场景下，请不要设置 `cors_origins`，自行添加中间件：

```python
from fastapi.middleware.cors import CORSMiddleware

app = create_app(agent, auth=my_auth)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chat.example.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

如果前后端属于不同的站点（而不只是同一站点的不同子域名），cookie 必须设为
`SameSite=None`，此时需要自行防范 CSRF：取消运行、立即执行定时任务、上传文件等接口是
无请求体或 multipart 的 POST，属于不触发 CORS 预检的“简单请求”。内置 token cookie 使用
`SameSite=Strict`，不受影响。

**登录跳转。** 内置页面无法显示你的登录表单，需要告诉它登录页在哪里。设置
`ui=ChatUI(login_url="/login?next={next}")` 后，只要 API 请求被你的依赖以 401 拒绝，浏览器
就会跳转到该地址，其中 `{next}` 会替换为当前页面经过 URL 编码的路径（含查询参数）。未设置
`login_url` 时，页面只提示用户未登录。短时间内多个 401 只会触发一次处理；内置 token 校验
返回的 401（错误码 `server_token`）仍然弹出 token 输入框。

`create_app()` 本身默认不启用认证；交给其他 ASGI 服务器运行时，请自行传入 `token` 或
`auth`。`serve()` 只检查由 `create_app()` 构建的应用；自行挂载 `build_api_router` 的应用
沿用自身的中间件，按原样运行。

## 部署在路径前缀下

反向代理把应用发布在 `/lovia/` 下、转发前剥掉该前缀时，需要把前缀告诉 uvicorn：

```nginx
location /lovia/ {
    proxy_pass http://127.0.0.1:8000/;   # 末尾的斜杠会剥掉 /lovia
}
```

```python
serve(app, root_path="/lovia")           # 或：lovia web --root-path /lovia
```

页面从每个请求中取得前缀，因此静态资源、API 请求、事件流和 token cookie 都限定在该前缀下；
同一主机上的两个实例各自保存 token，互不覆盖。静态资源地址不含协议和主机名，终止 TLS 的
代理也不会让它们变成被拦截的 `http://` 请求。服务端本身仍在根路径响应，请通过代理打开页面。

如果要把 `create_app()` 应用放进已有的 FastAPI 应用，可以直接挂载
（见 [HTTP API](http-api.md#挂载-create_app-应用)），前缀的处理方式相同。

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
| `cron` | Cron 表达式，按服务器本地时间匹配；`lovia[web]` 已包含 `croniter` |

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

插件还提供无需审批的 `list_schedules` 和 `cancel_schedule`。取消只会停用任务，不会删除记录。
停止条件通常由无人值守的定时运行自行处理，而这类运行中的
审批请求会被自动拒绝，因此 `cancel_schedule` 不能依赖审批。

## 安全检查

- 个人使用时保持 `host="127.0.0.1"`。`serve()` 在非回环地址上从不匿名开放 API；
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
