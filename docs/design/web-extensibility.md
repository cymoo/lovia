# Web 层可扩展性：实现方案

> 状态：待实现 · 2026-09-24 · 范围：`lovia/web`（不改 core）
>
> 背景：可扩展性系列 PR #212–#217 已合并。本文是对照当前代码（v0.9.70）重新核对之后的方案，前两轮讨论里的结论也一并复核过。

## 结论

分三个 PR 做，多用户隔离继续延后：

| PR | 内容 | 性质 | 规模 |
| --- | --- | --- | --- |
| 1 | `/healthz` 不再受鉴权拦截；修正鉴权和部署文档；补充"作为子应用挂载"时 lifespan 的写法 | fix + docs | 小 |
| 2 | 支持子路径部署（base path、根相对的资源 URL、cookie path）；非 token 鉴权收到 401 时跳转登录页 | feat | 中 |
| 3 | 模板覆盖：用户可提供模板目录，`index.html` 预留几个 block | feat | 小 |

## 1. 复核上一轮的结论

上一轮看的是 #212 之前的代码，有几条已经过时或需要改正：

| 上一轮的说法 | 现在的情况 | 处理 |
| --- | --- | --- |
| 优先导出 `lifespan(deps)` | #212 已经做了：`RouterDeps.lifespan` 已导出，`create_app` 自己也用它（[app.py:297](../../lovia/web/app.py#L297)），http-api.md 里有用法 | 移出计划 |
| 前端只在响应带 `WWW-Authenticate: Bearer` 时才弹 token 框 | #213 之后，token 校验失败会返回专门的错误码 `server_token`（[auth.py:82](../../lovia/web/auth.py#L82)）。自定义的 Bearer/JWT 鉴权通常也会带这个响应头，按响应头判断会误弹 | 改成看 `detail.code === "server_token"` |
| 前端要区分 401 和 403 | 403 已经有两种业务含义：`path_denied`（[workspace.py:300](../../lovia/web/api/workspace.py#L300)，[files.js:656](../../lovia/web/static/js/files.js#L656) 靠它提示"超出工作区"）和 `local_origin_required`（[config.py:160](../../lovia/web/api/config.py#L160)） | 不做全局的 403 处理；自定义 auth 返回的 403 按普通错误提示 |
| 前端有 48 处 `fetch('/api/...')` | 49 处 `fetch` 全在 api.js 里。api.js 之外只有 [sessions.js:149](../../lovia/web/static/js/sessions.js#L149) 的 `EventSource('/api/events')`，另外 3 处 `fetch` 用的是 api.js 提供的 URL 构造函数 | 在 api.js 里加一个内部 `request()` 就能统一处理 |
| 文档要写明"自定义鉴权必须支持 Cookie" | web-server.md 已经说明 `EventSource` 靠 cookie 鉴权，但讲 `auth=` 的那段没提 | PR 1 补一句 |
| `/api/config` 应该只允许管理员访问 | 只有 CLI 的默认 agent 模式会挂这些路由（`config_runtime`），嵌入式用法没有。只有一个 token 时，拿到 token 的人就是管理员 | 放到多用户设计里再处理 |
| SSE 经过反向代理可能被缓冲 | `sse-starlette` 默认发送 `X-Accel-Buffering: no`，并且每 15 秒发一次 ping | 不需要处理 |

## 2. 新发现的问题

下面每一条都用 TestClient 探针在当前代码上复现过。

**P-1　`auth=` 会把 `/healthz` 拦下来。** `/healthz` 定义在 `build_api_router` 里（[api/\_\_init\_\_.py:50](../../lovia/web/api/__init__.py#L50)），而 `create_app` 把鉴权依赖挂在整个 router 上（[app.py:326](../../lovia/web/app.py#L326)）。token 模式下，`OPEN_PATHS` 会放行它（[auth.py:42](../../lovia/web/auth.py#L42)、[:75](../../lovia/web/auth.py#L75)）；自定义的 `auth=` 不知道要放行，于是返回 401。可 [web-server.md:107](../en/web-server.md) 说它"保持公开"。结果是 K8s 之类的探针会把实例判为不健康。

**P-2　`create_app()` 作为子应用挂载时，lifespan 不会执行。** `parent.mount("/lovia", create_app(agent))` 之后，页面和 API 都能正常访问，但 Starlette 不会运行子应用的 lifespan。于是 scheduler 不启动，`sweep_stale_runs` 不执行，关服时也不做收尾。整个过程没有任何报错或日志。

**P-3　部署在子路径下时，前端请求全部 404。** 同样的挂载方式，或者反向代理加 `root_path`：页面和静态资源的地址是对的（`url_for` 会带上 root_path），但 api.js 里的 `/api/...` 和 `EventSource('/api/events')` 都是从根路径开始写的，所以全部 404。

**P-4　TLS 在代理上终止时页面可能白屏。** 模板里的 `url_for()` 生成的是带协议和主机名的完整 URL（`http://host/static/...`）。uvicorn 默认只信任来自 127.0.0.1 的 `X-Forwarded-Proto`（由 `forwarded_allow_ips` 控制）。代理不在本机时（容器、Ingress 之类），https 页面里就会引用 http 资源，浏览器会把脚本和样式当作混合内容拦截。

**P-5　token cookie 的 `path=/` 会让同一域名下的多个实例互相覆盖。** [main.js:39](../../lovia/web/static/js/main.js#L39) 把 path 写死成 `/`。如果两个实例挂在同一域名的 `/a/` 和 `/b/` 下（"一个租户一个进程"最常见的部署方式就是这样），它们的 `lovia_token` 会互相覆盖，结果就是不停地 401。

**P-6　非 token 鉴权下，前端的 401 处理是错的。** 前端只在 `loadAgents` 这一处处理 401，而且不管原因都弹出 token 输入框（[main.js:124](../../lovia/web/static/js/main.js#L124)）。用 `auth=` 接 OAuth 或 Session 时，这个输入框毫无意义。登录过期后，其他请求收到的 401 只会变成一条条零散的错误提示。

**P-7　deployment.md 已经过时。** 中英文版都还写着"lovia 本身不提供认证"和"内置服务不提供身份认证"（en 第 14、60 行，zh 第 13、58 行）。这和现在的实际行为矛盾：已经有 token 鉴权，而且 `serve()` 在非 loopback 地址上会拒绝没有鉴权的应用。

## 3. 方案

### PR 1：`/healthz` 与文档修正（fix）

代码：

- 把 `/healthz` 从 `build_api_router` 移到 `create_app`，直接注册在 app 上，不经过鉴权依赖。
- 删除 `auth.py` 里的 `OPEN_PATHS` 及对应分支，因为 router 里已经没有需要放行的路由了。
- 破坏性变化：自己挂载 `build_api_router` 的应用不再自带 `/healthz`（这类应用通常有自己的健康检查）。在 PR 描述里写明。

文档（中英文同步）：

- **deployment.md**：重写鉴权那一行和 danger 提示框，照实写出 `token`、`auth=` 以及 `serve()` 的行为。
- **web-server.md** 的鉴权一节补三点：
  1. 自定义 `auth=` 要配合内置 UI 使用，就必须接受 cookie，因为 `EventSource`、`<img>` 和下载链接都无法附带自定义请求头。
  2. `auth=` 只保护 lovia 自己的 API 路由。你自己 `include_router` 的路由要加上 `dependencies=[Depends(my_auth)]`。UI 页面和 `/api/docs` 是公开的；如果要保护整个站点，请用中间件或反向代理。
  3. 跨域并且用 cookie 鉴权时：不要传 `cors_origins`，而是自己调用 `app.add_middleware(CORSMiddleware, allow_origins=[...], allow_credentials=True, ...)`。另外要注意，`SameSite=None` 的 cookie 需要自己防 CSRF：取消运行、立即触发定时任务、上传文件这类接口是不带 JSON body 的 POST 或 multipart 请求，属于不会触发预检的"简单请求"。lovia 自己的 token cookie 是 `SameSite=Strict`，不受影响。
- **http-api.md**：
  - 补上作为子应用挂载时 lifespan 的写法（对应 P-2）：在父应用的 lifespan 里写 `async with sub.state.deps.lifespan(): yield`；
  - 说明 `/healthz` 改由 `create_app` 提供。

测试：

- `auth=` 拒绝所有请求时，`/healthz` 仍然返回 200；
- 只挂载 `build_api_router` 时没有 `/healthz`。

### PR 2：子路径部署与登录跳转（feat）

服务端：

- `index.html` 里所有的 `url_for(...)` 改成 `url_for(...).path`。这样生成的是带 root_path、但不带协议和主机名的根相对路径，P-4 也一并修掉。已验证：在 root_path 为 `/lovia` 时，生成的是 `/lovia/static/<token>/...`。
- `app_config` 新增两个字段：`base_path`（取 `request.scope["root_path"]`）和 `login_url`。
- 新增 `ChatUI` 并把 UI 选项收进去（见第 4 节的决策 1），其中新增的 `login_url` 只对 UI 生效。
- CLI 增加 `--root-path`，原样传给 uvicorn。
- http-api.md 里"挂载 `create_app()` 应用"的示例去掉 `ui=False`（PR 1 加的，因为那时内置页面只能部署在根路径）。

前端：

- **api.js**：用一个内部的 `request(path, init)` 替换直接调用 `fetch`，它负责两件事：
  - 在路径前拼上 base；
  - 遇到 401 时调用注册好的 `onUnauthorized(res)`。同一个页面只触发一次，防止并发请求重复弹框或重复跳转。

  对外导出 `configureApi({ base, onUnauthorized })`。三个 URL 构造函数也要拼上 base，另外新增 `eventsUrl()` 给 sessions.js 用。不调用 `configureApi` 时，行为和现在完全一样，所以 api.js 仍然可以作为独立的参考客户端使用。
- **main.js**：从 `app_config` 读取 `base_path` 后调用 `configureApi`。401 按错误码分三种情况处理：
  - `code === "server_token"`：沿用现在的 token 输入框，但 cookie 的 path 改为 `${base}/`，修掉 P-5。
  - 其他 401，并且配置了 `login_url`：执行 `location.assign(login_url)`。如果 `login_url` 里有 `{next}`，就替换成编码后的当前地址。
  - 其他 401，但没有配置 `login_url`：只提示一次"未登录或登录已过期"，不自动刷新，免得陷入死循环。

测试：

- Python：用 `TestClient(root_path="/lovia")` 检查，HTML 里的资源引用都以 `/lovia/static/` 开头且不带协议；`app_config` 里有 `base_path` 和 `login_url`。
- JS（`node --test`）：base 拼接正确；401 钩子只触发一次；其他状态码不触发。
- 端到端（verify skill）：用 `root_path=/lovia` 启动，在浏览器里从 `/lovia/` 进入，把聊天、事件流、文件预览和导出都走一遍。

### PR 3：模板覆盖（feat）

- 新增 `templates` 选项，值是一个目录。Jinja loader 用 `ChoiceLoader([用户目录, PrefixLoader({"lovia": 内置目录}), 内置目录])`。用户的 `index.html` 写 `{% extends "lovia/index.html" %}`，只覆盖需要改的 block。这和 MkDocs Material 的 `custom_dir` 是一个思路。
- `index.html` 里预留三个空 block：
  - `head`：放在 `</head>` 前面，用来加 CSS 和 meta；
  - `sidebar_footer`：侧栏底部，用来放用户菜单或"退出登录"链接；
  - `body_end`：放在 main.js 后面，用来加脚本。
- `ui.py` 里模块级的 `_TEMPLATES` 改成每个 router 各自创建，因为模板目录是按 app 配置的。
- 以下约定写进文档：
  - **换主题**：覆盖 styles.css 里的 CSS 变量（`--bg`、`--accent` 等），这是正式支持的方式。CSS 类名和 DOM id 不保证稳定。
  - **自己的静态资源**：`app.mount("/brand", StaticFiles(directory=...), name="brand")`，模板里用 `url_for('brand', path=...).path` 引用。一行就能搞定，所以不为它加参数。
  - **JS 扩展**：不提供前端插件 API。api.js 是唯一有文档的客户端；在 `body_end` 里 import 其他内部模块，出了问题自己负责。

测试：

- 覆盖一个 block 后，页面里能看到注入的内容，内置的 DOM id 也都还在；
- 不传 `templates` 时，渲染结果不变。

## 4. 已定的决策

三项都按推荐执行（2026-09-24）：

1. **合并 UI 选项。** 在 PR 2 中完成。`create_app` 现在有 28 个参数，其中只给 UI 用的有 `empty_title`、`empty_description`、`empty_examples` 三个，这次再加上 `login_url` 和 `templates` 就是五个。改为 `ui: bool | ChatUI = True`，`ChatUI` 是一个 frozen dataclass，装下这五个选项。`title` 留在顶层，因为 `/api/info` 和 FastAPI 也用它。`build_ui_router` 也改为接收 `ChatUI`。这是破坏性变化（1.0 之前可以接受），两份 README 都要同步。`templates` 在 PR 3 加进 `ChatUI`。
2. **`login_url` 支持 `{next}` 占位符。** 前端只需要多写一行，登录回来不会丢掉 `?session=` 这类深链接。
3. **CLI 增加 `--root-path`。** 在 PR 2 中完成，原样传给 uvicorn。

## 5. 明确不做的

| 提议 | 不做的原因 |
| --- | --- |
| 前端插件 API，或把 `store` 暴露出去 | 会让内部模块变成需要长期兼容的接口；`body_end` 加上 api.js 已经够用 |
| 加一个 `cors_credentials` 参数 | 用户自己写只要 3 行（PR 1 写进文档） |
| 让 `auth=` 也保护 `GET /` | 依赖只能返回 401，没法跳转到登录页。要保护整个站点，用中间件或代理；其他情况由 `login_url` 处理 |
| 加一个额外静态目录的参数 | 一行 `app.mount` 就够了 |
| 导出 `build_ui_router` | 想把内置 UI 放进已有应用，把 `create_app()` 作为子应用挂进去就行（PR 1 补文档，PR 2 支持子路径）。导出它还得把静态资源挂载和 asset token 这些细节一起暴露出去 |
| `/api/info` 返回当前用户 | 先得有"身份"这个概念，放到多用户设计里。只是要一个"退出登录"链接的话，用 `sidebar_footer` 就能做 |
| 子应用的 lifespan 没执行时自动告警 | 不用 `with` 的 TestClient 也不会执行 lifespan，测试里会满屏告警。把文档写清楚就够了 |
| 可配置 `docs_url`，或隐藏 OpenAPI | schema 里没有业务数据，等有实际需求再做 |

## 6. 多用户隔离（继续延后）

**先用进程级隔离。** 一个租户一个进程，各自使用独立的 `db_path`、工作区根目录、Memory 目录和 `token`/`auth`。如果要挂在同一域名的不同子路径下，需要 PR 2 的 base path 和 cookie path。剩下一点小问题：同源的多个实例共用 localStorage（草稿、run 完成通知的时间戳），影响很小。

**只有出现下面两种情况之一，才开始设计单进程多用户：** 租户多到进程管理不过来；或者需要跨用户的视图，比如管理后台。到时候的设计要点沿用上一轮：

- 由 `auth` 依赖把身份写进 `request.state`，作为获取当前用户的接口；
- 三张表都加 owner 列（`_ADDED_COLUMNS` 可以承接这类加列迁移）；
- 用一个统一的 `require_session` 检查会话归属；
- 事件总线按 owner 过滤；
- 定时任务记录 owner，执行时沿用；
- 为每个用户 `clone` 一份 agent，Memory 目录和工作区按用户分开；
- `/api/config` 只允许管理员访问。

## 7. 实施约定

- 每个 PR 单独发版（按发布流程升 patch 版本），commit 用 Conventional Commits，squash merge。
- 公开行为有变化时，同步更新 README.md、README-zh.md，以及 docs/en 和 docs/zh 下对应的页面（web-server、http-api、deployment、web-ui）。
- 验证：`pytest tests/web`、`node --test tests/web/js/*.test.mjs`、`npx -p typescript@7.0.2 tsc -p lovia/web/static/jsconfig.json`；涉及 UI 的改动再用 verify skill 跑一遍端到端。
