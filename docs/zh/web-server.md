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
| `lifespan` | `None` | 你自己资源的生命周期，写法同 `FastAPI(lifespan=...)`（见下文） |
| `run_context` | `None` | 用页面上下文和请求中的身份，为每次运行构造 `RunContext.deps`（见[运行上下文](#运行上下文)） |
| `title` | `"lovia"` | 页面标题，也是 API 报告的标题 |

`create_app` 自己占用了应用的 FastAPI `lifespan`，用来启停调度器、收尾仍在运行的 Run。
工具要用的资源（数据库连接池、HTTP 客户端）想随应用一起打开和关闭，就把你自己的
lifespan 传给 `lifespan=`。它包在 lovia 的外层：资源在第一个 Run 之前就绪，并且等运行中的
Run 都收尾之后才关闭。你 yield 的 state 会照常出现在 `request.state` 上。

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    await db.connect()
    yield
    await db.close()

app = create_app(agent, lifespan=lifespan)
```

`ChatUI` 的选项（用于 `ui=ChatUI(...)`）：

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `empty_title` / `empty_description` | lovia 默认文案（随用户语言） | 空白聊天页的文案；说明可以是若干短句组成的列表 |
| `empty_examples` | `()` | 空白聊天页上的示例问题；点击后填入输入框，但不会自动发送 |
| `login_url` | `None` | 使用 `auth=` 时未登录用户的跳转地址（见[认证](#认证)） |
| `assets` | `None` | 你的 UI 目录：`index.html` 覆盖模板、样式表、图片、脚本（见[定制页面](#定制页面)） |
| `scripts` | `()` | 页面启动前导入的 JS 模块，例如组件渲染器（见[扩展脚本](#扩展脚本)） |
| `styles` | `()` | 在内置样式表之后加载的样式表 |
| `logo` / `favicon` | `None` | 替换侧栏标题的图片 / 标签页图标 |
| `messages` | `{}` | 按语言提供的界面文案：覆盖内置文案、新增语言、给自己模板里的元素用（见[文案与语言](#文案与语言)） |
| `default_lang` | `None` | 用户还没在设置里选过语言时使用的语言；`None` 表示跟随浏览器 |

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

把自己的文件放进一个目录，用 `ChatUI(assets=...)` 指向它。`lovia web init-ui ui` 可以生成一个起步
目录：列出全部 block 的 `index.html`、列出全部设计变量及默认值的 `theme.css`，以及带类型检查的
`extension.js`——内容都是注释掉的，生成后页面不会有任何变化。

```text
ui/
├── index.html    # 可选：继承内置页面，填写其中的 block
├── theme.css
├── logo.svg
├── favicon.png
└── acme.js
```

```python
from lovia.web import ChatUI, create_app

app = create_app(
    agent,
    title="Acme",
    ui=ChatUI(
        assets="ui",
        styles=["theme.css"],
        logo="logo.svg",
        favicon="favicon.png",
        scripts=["acme.js"],
    ),
)
```

lovia 把这个目录挂在 `<app>/ui/` 下。除模板（`*.html`）和以点开头的文件外，目录里的
文件都是公开的；浏览器每次加载都会重新验证，所以改完文件刷新即可看到，不用重启服务。
`scripts`、`styles`、`logo` 和 `favicon` 里的每一项可以是：

- `assets` 里的文件：`"theme.css"`；
- 应用上的路径：`"/brand/theme.css"`，用于你自己提供的文件；部署在
  [路径前缀下](#部署在路径前缀下)时依然有效；
- URL：`"https://cdn.example.com/theme.css"`。

除了这些文件，在内置页面的基础上扩展还靠下面几种公开接口，各管一件事：

| 接口 | 用途 |
| --- | --- |
| [模板 block](#模板-block) | 在固定位置放入自己的内容：链接、输入框旁的控件、公告 |
| [设计变量](#样式) | 颜色、字体、圆角、间距 |
| [`data-part` 属性](#样式) | 在自己的 CSS 里定位某个区域：隐藏它，或改它的样式 |
| [`data-i18n` key](#文案与语言) | 文案与语言 |
| [`window.lovia`](#扩展脚本) 和 `api.js` | 你的脚本；调用 API |

其余一切——CSS 类名、元素 id、JavaScript 模块——都属于内部实现，可能随版本变化。

两个可运行的示例把这些串了起来：`examples/32_web_branding.py` 给页面换上品牌（logo、设计变量、
block、文案、新增语言），`examples/33_web_extensions.py` 通过 `window.lovia` 扩展页面行为
（工具视图、事件、页面动作）。

### 模板 block

`assets` 里的 `index.html` 会替换内置页面。继承内置页面，只填写需要的 block 即可：

```html+jinja
{# ui/index.html #}
{% extends "lovia/index.html" %}

{% block sidebar_nav %}
<a href="/help" data-i18n="acme.help">Help center</a>
{% endblock %}

{% block welcome %}
{{ super() }}
<p class="acme-notice">今晚 22:00 例行维护。</p>
{% endblock %}

{% block sidebar_footer %}
<a class="signout" href="/logout">退出登录</a>
{% endblock %}
```

| Block | 渲染位置 |
| --- | --- |
| `head` | `<head>` 末尾：meta 标签、预加载 |
| `sidebar_header` | 侧栏顶部；它包住了标题（或 `logo`），填写后两者都会被替换，比如放一个 logo 再配上名字 |
| `sidebar_nav` | 侧栏导航末尾、"设置"下方：你自己的链接 |
| `sidebar_footer` | 侧栏底部：用户菜单、退出登录链接 |
| `topbar_actions` | 顶栏按钮组的最左侧 |
| `composer_actions` | 输入框内、文本框之后：模式切换、选择器 |
| `welcome` | 空白聊天页的内容；`{{ super() }}` 保留默认的标题、说明和示例问题。点"新对话"时页面会恢复它 |
| `body_end` | 应用脚本之后：你自己的脚本 |

`sidebar_nav` 里的链接按内置导航项的样式绘制，指向当前[视图](#视图)的那一个显示为选中。
在模板里引用 `assets` 中的其他文件，用
`{{ url_for('ui-assets', path='banner.png').path }}`，它会带上路径前缀。

### 样式

内置的 `styles.css` 放在名为 `lovia` 的 cascade layer 里。你自己的样式表（不放进任何
layer）中的规则总会压过内置规则，不管选择器的优先级如何，不需要 `!important`。

更换主题时，覆盖 `styles.css` 开头的设计变量。两个主题各有一套取值，所以要分别设置；
直接写在 `:root` 上的变量对两个主题都生效。`--accent-hover`、`--accent-soft`、
`--accent-text` 由 `--accent` 推导，换品牌色通常只需设 `--accent`，需要时再单独覆盖：

```css
/* ui/theme.css */
[data-theme="light"] { --accent: #6d28d9; }
[data-theme="dark"]  { --accent: #a78bfa; }
:root { --sans: "Inter", system-ui, sans-serif; --radius: 8px; }
```

| 变量 | 作用 |
| --- | --- |
| `--bg`、`--surface`、`--surface-2`、`--surface-3`、`--border`、`--border-strong` | 画布、面板、卡片、悬停态及其边框 |
| `--ink`、`--ink-2`、`--muted`、`--muted-2` | 文字，由深到浅 |
| `--accent`、`--accent-hover`、`--accent-soft`、`--accent-text` | 品牌色及其衍生色（后三个默认由 `--accent` 推导） |
| `--user-surface`、`--user-border` | 用户消息气泡 |
| `--chart-1` … `--chart-8` | 图表系列颜色 |
| `--warn`、`--danger`、`--ok` | 状态色 |
| `--sans`、`--mono` | 字体 |
| `--radius-xs` … `--radius-xl` | 圆角 |
| `--sidebar-w`、`--content-w` | 侧栏宽度、对话区的阅读宽度 |

要定位某个区域，就选它的 `data-part`：

```css
[data-part="export"] { display: none; }                                   /* 去掉导出菜单 */
[data-part="tool"][data-tool="read_process_output"] { display: none; }   /* 隐藏某个工具的卡片 */
[data-part="turn"][data-role="user"] .bubble { border-radius: 4px; }
```

| 区域 | `data-part` |
| --- | --- |
| 侧栏 | `sidebar`、`sidebar-header`、`agent-switcher`、`sidebar-nav`、`new-chat`、`memory`、`schedules`、`settings`、`sessions-label`、`sessions-search`、`sessions` |
| 顶栏 | `topbar`、`chat-title`、`topbar-actions`、`tasks`、`export`、`files-toggle` |
| 视图 | `view`（打开的[视图](#视图)画在这个容器里） |
| 对话区 | `transcript`、`welcome`、`todo-panel`、`turn`（带 `data-role="user"` 或 `"assistant"`）、`tool`（带 `data-tool="<工具名>"` 和 `data-status`：`running`、`awaiting`（等待用户，比如在提问），之后是 `done` 或 `error`）、`activity`（连续两个及以上的思考 / 工具步骤折叠成的一行摘要；回复正文开始后自动收起）、`message-actions`（一条消息的[操作](#消息操作)；每个按钮带 `data-action="<id>"`） |
| 输入框 | `composer`、`attach`、`context-ring` |
| 其他 | `files-panel`；设置面板里的 `language`（语言那一行） |

隐藏某个工具的卡片不会连带隐藏它的审批请求：审批卡片是单独渲染的。

### 文案与语言

界面自带英文和中文。`ChatUI(messages=...)` 可以修改文案、新增语言，也可以给自己模板里的
元素提供文案：

```python
ui = ChatUI(
    default_lang="zh",
    messages={
        "en": {"composer.placeholder": "Ask Acme anything…", "acme.help": "Help center"},
        "zh": {"composer.placeholder": "问问 Acme…", "acme.help": "帮助中心"},
        "ja": {"composer.placeholder": "何でも聞いてください…", "nav.newChat": "新しいチャット"},
    },
)
```

- key 见 `lovia/web/static/js/i18n.js`（`nav.newChat`、`composer.placeholder` 等）。如果在界面
  自带的前缀下写了一个界面没有的 key（拼错了，或者新版本改了名），浏览器 console 里会有警告。
- 界面没带的语言（上例的 `ja`）会出现在设置的语言列表里；缺少的文案回退到英文。
- 自己前缀下的 key（`acme.help`）通过 `data-i18n`（文本）、`data-i18n-title`、
  `data-i18n-aria` 和 `data-i18n-placeholder` 给模板里的元素提供文案。当前语言没有对应
  文案时，元素保留自己原本的文字。
- `default_lang` 在用户于设置里选择语言之前生效，选"自动"即回到它。想让用户固定使用一种
  语言，就设置它并隐藏语言选项：`[data-part="language"] { display: none; }`。

### 扩展脚本

`ChatUI(scripts=[...])` 列出的 ES 模块会在页面**启动之前**导入，所以它们注册的内容（比如
[组件渲染器](#ui-组件)、[工具视图](#工具视图)）在首次渲染时就已就位。脚本通过 `window.lovia`
和页面打交道，这是页面唯一公开的脚本接口；其余模块都属于内部实现。

| `window.lovia` | 说明 |
| --- | --- |
| `version` | 提供页面的 lovia 版本，用于特性检测 |
| `ready` | 应用启动结束时兑现的 promise：页面已就绪，首次的对话列表请求和 URL 指定的对话都已加载完（或失败），URL 指定的[视图](#视图)已打开。页面发现用户未登录时一直挂起 |
| `state` | 冻结的快照：`sessionId`（新对话为 null）、`agent`、`theme`、`lang`、`busy`（是否有运行正在流式输出）、`view`（打开的[视图](#视图)，没有时为 null）。需要最新值时重新读取 |
| `on(event, fn)` | 监听[事件](#事件)；返回一个取消监听的函数 |
| `send(text)` | 以用户身份发送 `text`；有运行在进行时，会排队到下一轮 |
| `setDraft(text)` | 把 `text` 放进输入框，由用户修改后发送 |
| `newChat()` / `openSession(id)` | 打开一个新对话 / 打开对话 `id`，并关闭视图 |
| `toast(text, { type })` | 短暂提示；`type` 为 `"info"`（默认）或 `"error"` |
| `api` | 页面的 API 客户端，已处理[路径前缀](#部署在路径前缀下)和登录 |
| `fetch(path, init)` | 用来请求你自己的路由的 `fetch`：`path` 以 `/` 开头，会加上路径前缀；遇到 401 时走页面的登录处理 |
| `t(key, params)` | 当前语言下的界面文案，包括你[自己的 key](#文案与语言) |
| `registerRenderer(kind, renderer)` / `getRenderer(kind)` | [UI 组件](#ui-组件) |
| `registerToolView(name, view)` | [工具视图](#工具视图) |
| `registerMessageAction(id, action)` / `refreshMessageActions()` | [消息操作](#消息操作) |
| `registerView(id, view)` / `openView(target)` / `closeView()` | [视图](#视图) |
| `provideContext(key, fn)` | 把 `key` 加进运行开始时的上下文，发送时读取 `fn()` 的值，见[运行上下文](#运行上下文) |

```js
// ui/acme.js
const { lovia } = window;

lovia.ready.then(() => {
  console.info(`Acme extensions on lovia ${lovia.version}`);
});

// 模板在 composer_actions block 里放的按钮。
document.getElementById('acme-summarize')?.addEventListener('click', () => {
  lovia.send(lovia.t('acme.summarizePrompt'));
});
```

不要在脚本顶层 `await lovia.ready`：页面要等你的脚本加载完才启动，两边会互相等待，直到下面说的
5 秒上限。加载失败的脚本会弹出错误提示，页面不带它照常启动。5 秒后仍在加载的脚本不再阻塞启动，
它会继续加载，加载完再注册。脚本是你自己的代码，拥有页面的全部权限，所以第三方代码请放进
`assets` 自托管，不要直接引用别人的 CDN。

想要编辑器补全和类型检查，可以引用 lovia 自带的类型声明（路径在你的环境的 `site-packages` 下）：

```js
// @ts-check
/// <reference path="…/site-packages/lovia/web/static/lovia.d.ts" />
```

### 事件

事件描述的是屏幕上这个页面的动静。抛出异常的监听器会被记录下来，不影响其他监听器。

| 事件 | payload | 触发时机 |
| --- | --- | --- |
| `session` | `{ sessionId }` | 屏幕上换了一个对话（`null` 表示新对话）；新对话首次保存时也会触发 |
| `agent` | `{ agent }` | 用户切换了 agent |
| `run-start` | `{ sessionId }` | 一次运行开始在对话里流式输出：用户发送，或刷新后重新接上 |
| `run-end` | `{ sessionId, status, usage }` | 对话不再流式输出这次运行。`status`：`done`（已回答）、`error`、`cancelled`（用户停止）或 `detached`（用户切换了对话或连接断开，服务端可能还在运行）。`usage` 是已回答运行的 token 用量，否则为 `null` |
| `theme` | `{ theme }` | 浅色或深色主题生效 |
| `view` | `{ view }` | 打开了一个[视图](#视图)（如 `'exec/perf'`），或回到了对话（`null`） |

```js
lovia.on('run-end', ({ status, usage }) => {
  if (status === 'done') analytics.track('reply', { tokens: usage?.total_tokens });
});
```

### 工具视图

每次工具调用都显示为一张卡片：一行写着名字和参数，展开后是参数和结果。`registerToolView`
可以改变某个工具卡片的呈现方式——适合你无法控制的工具（比如 MCP 服务器），也适合面向终端用户的
部署：`order_status(id: …)` 对他们没有意义。lovia 自带的工具已经这样显示——「读取文件 a.md」
「$ ls -la」「网页搜索 “…”」——你注册的视图（包括 `'*'` 兜底）优先于它们：

```js
// 代替 name(args) 的一行摘要，作用于所有没有专属视图的工具；返回 null 交给下一层
// （lovia 自带工具有自己的一行），最后才是 name(args)。
const labels = { order_status: '正在查询订单' };
lovia.registerToolView('*', { summary: (tool) => labels[tool.name] ?? null });

lovia.registerToolView('web_search', {
  summary: (tool) =>
    tool.status === 'done' ? `已搜索「${tool.args?.query}」` : `正在搜索「${tool.args?.query}」…`,
  // 绘制卡片主体；`tool` 每次变化都会重新调用。
  render(el, tool) {
    if (tool.status !== 'done') return;
    const list = document.createElement('ol');
    for (const hit of JSON.parse(tool.result).results ?? []) {
      const link = document.createElement('a');
      link.href = hit.url;
      link.target = '_blank';
      link.textContent = hit.title; // 不要用 innerHTML：工具输出不可信
      const item = document.createElement('li');
      item.append(link);
      list.append(item);
    }
    el.replaceChildren(list);
  },
});
```

| `view` 字段 | 说明 |
| --- | --- |
| `summary(tool)` | 代替 `name(args)` 的一行摘要；返回 `null` 或 `undefined` 时依次交给 `'*'` 视图、lovia 自带的一行、`name(args)`。鼠标悬停时显示它代表的调用 |
| `render(el, tool, ctx)` | 把卡片主体画进 `el`。约定和[组件渲染器](#ui-组件)相同（`ctx`、可选的清理函数），原始结果仍可通过 "Source" 开关查看 |
| `snapshot(tool, ctx)` | 用于对话 HTML 导出的静态 HTML |

- `tool` 是这次调用的冻结快照：`id`、`name`、`args`（已解析；超过 256 KB 或无法解析时没有）、
  `status`（`running`、`awaiting` 等待用户、`done`、`error`）、`result`（结束后的输出文本）、
  `isError`、`images`。它每次变化，视图的函数都会重新调用。
- 工具名的专属视图优先于 `'*'` 视图，而且逐个字段回退：只有 `summary` 的视图仍会用 `'*'` 视图的
  `render`。
- 结果本身是 [UI 组件](#ui-组件)的，仍按那个组件绘制；视图的 `summary` 照样生效。
- 隐藏某个工具的卡片是 CSS 的事（`[data-part="tool"][data-tool="…"]`，见[样式](#样式)），
  不归视图管。
- 以 `lovia.` 开头的 kind 归 lovia 自己使用，不要在这些名字下注册渲染器。

### 消息操作

`registerMessageAction(id, action)` 在消息底部的复制按钮旁加一个按钮：给回答评分、举报、存到笔记……
lovia 不保存这些数据，由操作去调用你自己的路由：

```js
const ratings = new Map(); // ref → 'up' | 'down'，屏幕上这个对话的评分

lovia.registerMessageAction('rate-up', {
  label: '回答得好',
  icon: '👍',
  when: (msg) => msg.role === 'assistant' && msg.ref !== null,
  pressed: (msg) => ratings.get(msg.ref) === 'up',
  async run(msg) {
    const res = await lovia.fetch('/feedback', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session_id: msg.sessionId, ref: msg.ref, rating: 'up' }),
    });
    if (!res.ok) throw new Error('评分保存失败'); // 以提示框显示
    ratings.set(msg.ref, 'up');
    lovia.refreshMessageActions(); // 画成按下状态
  },
});
```

| `action` 字段 | 说明 |
| --- | --- |
| `label` | 悬停提示和无障碍名称 |
| `icon` | 内置图标名、SVG 标记，或 emoji 之类的文字；不提供时显示 `label` |
| `when(msg)` | 这条消息是否显示该操作；不提供时所有消息都显示 |
| `pressed(msg)` | 按钮是否画成按下状态（`aria-pressed`），比如已经给过的评分 |
| `run(msg)` | 点击时调用；可以是 async，结束前按钮处于禁用状态。抛出的错误以提示框显示 |

- `msg` 是冻结的快照：`sessionId`、`role`（`user` 或 `assistant`）、`text`（Markdown，即复制得到的
  内容）和 `ref`。
- `ref` 标识一条用户消息，形如 `"<run_id>:<n>"`；回复沿用它所回答的那条消息的 `ref`，所以
  `(sessionId, ref)` 就是你为某条消息保存数据时的键。它在刷新后不变，也绝不会指向别的消息：回退会
  让一条消息的 `ref` 永久作废，修改后的问题、重新生成的回答都会拿到新的 `ref`，旧评分不会落到新内容上。
- 服务端给消息命名之前，`ref` 为 `null`：运行结束时命名；运行失败或被停止的，在下次加载这个对话时
  命名（对话存储不保存 checkpoint 时，所有运行都是如此）。用 `when: (msg) => msg.ref !== null`
  可以让操作等到那时再出现；不需要 `ref` 的操作（引用、翻译）立即显示。
- 操作只显示在已经定稿的消息上：还在流式输出的回复、还在排队的消息都没有。
- `when` 和 `pressed` 在绘制消息底部时调用。它们依赖的状态变化后（保存了评分、为刚打开的对话取回了
  评分），调用 `lovia.refreshMessageActions()`。用同一个 `id` 再次注册会替换原来的操作，传 `null`
  则移除。

完整示例见 `examples/35_web_feedback.py`：回答上的 👍 / 👎 由示例自己的路由存进 SQLite，另有报表
页面和一个"引用"操作。

### 视图

视图是你自己的页面，显示在主区域、替代聊天：仪表盘、配置表单、记录列表……注册之后放一个链接，
普通链接就能打开它：

```html+jinja
{% block sidebar_nav %}
<a href="?view=dashboard">仪表盘</a>
{% endblock %}
```

```js
lovia.registerView('dashboard', {
  title: '仪表盘',
  async render(el, ctx) {
    const res = await lovia.fetch('/acme/stats');
    el.replaceChildren(drawStats(await res.json(), { tab: ctx.path }));
    const off = lovia.on('theme', redrawCharts);
    return off; // 视图关闭或重新绘制前调用
  },
});
```

| `view` 字段 | 说明 |
| --- | --- |
| `title` | 视图打开时顶栏显示的标题 |
| `render(el, ctx)` | 把视图画进 `el`，每次打开、子路径每次变化时调用。可以是 async，可以返回清理函数。`ctx.path` 是 `?view=<id>/<path>` 里 id 之后的部分（没有时为 `''`） |

- **视图盖在对话上面，而不是离开对话。** 对话仍是当前对话（`state.sessionId`），运行照常流式
  输出；视图的状态和对话一起放在 URL 里：`?session=…&view=dashboard`。
- **链接。** 普通点击指向 `?view=<id>[/<path>]` 的链接，就在原地打开视图；中键或 ⌘/Ctrl 点击
  会在新标签页打开，新页面启动后直接显示该视图。`?view=`（空值）回到对话。请写成上面的相对形式，
  这样部署在[路径前缀下](#部署在路径前缀下)也成立。消息里的链接不会被拦截：模型输出不能驱动导航。
- **回到对话。** 点击任意对话（包括当前对话）、开始新对话、`lovia.openSession`、`lovia.newChat`
  和 `lovia.closeView()` 都会关闭视图。`lovia.send` 不会：消息照常发到下面的对话。视图打开期间
  如果对话开始等用户（审批、提问），页面会提示。在手机上，打开视图或对话都会收起侧栏抽屉。
- **显示什么。** 顶栏保留，显示视图的标题和你在 `topbar_actions` 里放的内容；属于对话的部分会隐藏。
  视图画在 `[data-part="view"]` 里，可以给它加样式。`sidebar_nav` 里指向当前视图的链接带有
  `aria-current="page"`（按 id 比较，所以打开 `exec/perf` 时 `?view=exec` 仍显示选中）。
- **生命周期。** 每次绘制前先执行上一次的清理函数。`render` 抛错时视图里显示一条提示；异步
  `render` 完成时视图已被切走，就立即调用它返回的清理函数。用同一个 `id` 再次注册，正打开的视图
  会重新绘制；传 `null` 则移除。切换主题不会重新绘制视图（会丢掉表单输入和滚动位置），需要跟随
  主题时监听 `theme` 事件。
- `openView(target)` 遇到没有注册的 id 会抛错；链接指向这样的 id、或启动时 URL 里是这样的 id，
  则只显示一条提示。id 只能用 `a-z`、`0-9` 和 `-`，以 `lovia` 开头的留给 lovia 自己。

## UI 组件

工具结果可以渲染成 UI（可排序的表格、指标卡片等），而不只是文本。工具返回
`ui_result(kind, props)`，前端由注册给该 `kind` 的渲染器把它画在工具卡片里；实时对话和重新
打开会话时都一样。

```python
from lovia import tool
from lovia.web import ui_result

@tool
def top_products(n: int = 10) -> str:
    """畅销商品，以表格形式展示给用户。"""
    rows = query_top_products(n)
    return ui_result("table", {"columns": ["sku", "units"], "rows": rows})
```

内置了三种 kind：`table`（上例，见[表格](web-ui.md#表格)）、`chart`（见[图表](web-ui.md#图表)），
以及在沙箱中运行的页面 `html`（见[页面](web-ui.md#页面)）。
上面表格里的数字直接来自查询，模型不需要把它们写出来。

这个信封就是工具的输出，所以模型同样能看到 props。如果调用参数本身就是要展示的数据（纯展示型
工具），就用 `from_args=True`（或某个字段名）代替 `props`，数据就不会再回传一遍。大块数据可以用
[`put_ui_data`](#大块数据) 挡在模型上下文之外。信封要控制在 Agent 的 `max_tool_output_chars`
之内（默认 20 万字符）：超长的输出会被截断，JSON 随之损坏，卡片就只能按文本显示。

自定义的 kind（比如 `return ui_result("kpis", {"items": [...]})`）需要你写一个渲染器：一个通过
`ChatUI(scripts=[...])` 加载的 JS 模块：

```js
// ui/kpis.js
window.lovia.registerRenderer('kpis', {
  render(el, props, ctx) {
    for (const item of props.items) {
      const card = document.createElement('div');
      card.className = 'kpi';
      card.textContent = `${item.label}: ${item.value}`; // 不要用 innerHTML
      el.append(card);
    }
    // 可以返回一个清理函数（释放 observer、定时器、图表实例等）。
  },
});
```

```python
app = create_app(agent, ui=ChatUI(assets="ui", scripts=["kpis.js"]))
```

渲染器收到三个参数：要绘制的元素、props 和一个上下文：

| `ctx` 字段 | 说明 |
| --- | --- |
| `theme` / `lang` | `'light'` 或 `'dark'`；`'en'` 或 `'zh'`。切换主题时，组件会先清理再重新渲染 |
| `basePath` | 挂载前缀，用于请求你自己的接口 |
| `sessionId` / `callId` | 组件所属的会话和工具调用 |
| `live` | 结果刚刚到达时为 `true`，重新打开聊天回放时为 `false`，可用于把开销大的操作推迟到用户点击之后 |
| `send(text)` | 以用户身份发送一条消息，例如"下钻 3 月"按钮。只在真实的点击或按键中生效，其他情况返回 `false` |

`render` 可以是 async 函数。渲染出错时卡片会提示，原始输出仍可通过卡片上的"查看原始数据"按钮
查看。某个 kind 还没有渲染器时先显示提示，等渲染器注册后自动补上，所以加载顺序无所谓。重复
注册同一个 kind 会替换原来的渲染器。

导出的 HTML 是不含脚本的独立文件，`render` 在那里用不上。可以再提供一个
`snapshot(props, ctx)`，把组件返回成静态 HTML（同步或 async 均可；`ctx` 相同，只是没有
`send`），导出时会对它做净化。没有 snapshot 的组件，导出时显示原始输出。内置表格会导出成
包含全部行的普通表格，图表导出成 SVG 图片。

```js
window.lovia.registerRenderer('kpis', {
  render(el, props, ctx) { /* … */ },
  snapshot(props) {
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
    return props.items.map((i) => `<p><b>${esc(i.label)}</b>: ${esc(i.value)}</p>`).join('');
  },
});
```

!!! warning "props 不可信"
    props 可能来自模型、工具、MCP 服务器或远程内容。请用 `textContent` 输出（或用页面上的
    `DOMPurify` 过滤），不要当作 HTML 插入。

注册内置的 kind 会替换它：比如注册你自己的 `chart` 渲染器，就能换一个图表库来画图。
`examples/31_web_components.py` 把内置的 `table` 换成了自己的版本，给每一行加了一个"追问"
按钮，通过 `ctx.send` 发出后续问题。

### 大块数据

信封是模型上下文的一部分，每一轮都会带上。结果太大时（比如一万行的查询结果），可以把完整
props 随聊天存起来，信封里只放一份预览：

```python
from lovia import tool
from lovia.web import put_ui_data, ui_result

@tool
async def run_query(sql: str) -> str:
    """执行只读查询，并以表格展示结果。"""
    columns, rows = await db.fetch(sql)
    ref = await put_ui_data({"columns": columns, "rows": rows})
    preview = {"columns": columns, "rows": rows[:20], "row_count": len(rows)}
    return ui_result("table", preview, data=ref)
```

模型只看得到预览，所以要在预览里放上它谈论那些没看到的行时需要的信息，比如总行数或合计。
页面会通过 `GET /api/sessions/{id}/ui-data/{ref}` 取回完整 props（与其他 API 使用同样的
鉴权），再交给渲染器。内置的和你自己的 kind 都能这样用，渲染器无需任何改动；导出的 HTML
也会画出完整数据。

- 数据与所属聊天同生命周期：删除聊天时一并删除。数据不在了，卡片会先显示提示，再显示预览
  （预览为 `None` 时只显示提示）。
- 在 `lovia web` 之外运行时（比如脚本或评测），`put_ui_data` 返回 `None`，信封就只是预览。
- 单份数据上限为 8 MiB 的 JSON。超出时抛出工具错误，模型可以据此缩小查询范围。

### 包装已有的渲染器

`window.lovia.getRenderer(kind)` 返回当前注册给 `kind` 的渲染器，即它的
`{ render, snapshot }`（没有则为 `undefined`），于是一个渲染器可以建立在另一个之上。内置渲染器
在你的脚本运行之前就已注册。例如，你自己定义的一个精简 kind 可以交给内置的 chart 来画，实时
对话和导出中都能显示：

```js
const chart = window.lovia.getRenderer('chart');
// {label, points: [[x, y], ...]} → 一个 ECharts option。
const option = (p) => ({
  title: { text: p.label },
  dataset: { source: p.points },
  xAxis: { type: 'category' },
  yAxis: {},
  series: [{ type: 'line' }],
});

window.lovia.registerRenderer('trend', {
  render: (el, props, ctx) => chart.render(el, option(props), ctx),
  snapshot: (props, ctx) => chart.snapshot(option(props), ctx),
});
```

拿到的是调用那一刻注册的渲染器，所以包装渲染器也可以替换它所包装的 kind（比如围绕内置的
`chart` 重新注册 `chart`），而不会调用到自己。

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

## 运行上下文

工具常常需要知道是谁在提问，以及用户在页面上选了什么——比如输入框旁下拉框里选的知识库。
`create_app(run_context=fn)` 用这两样东西为每次运行构造
[`RunContext.deps`](agents.md#注入应用依赖)：

```python
from dataclasses import dataclass

from fastapi import HTTPException, Request
from lovia import RunContext, tool
from lovia.web import RunTrigger, create_app


async def auth(request: Request) -> None:
    request.state.user = await users.from_session_cookie(request)  # 你的身份体系


@dataclass
class Deps:
    user: str
    kb: str


def run_context(trigger: RunTrigger) -> Deps:
    user = trigger.request.state.user if trigger.request else "scheduler"
    kb = trigger.client.get("kb", "docs")
    if kb not in KNOWLEDGE_BASES:
        raise HTTPException(400, f"unknown knowledge base {kb!r}")
    return Deps(user=user, kb=kb)


@tool
async def search(ctx: RunContext[Deps], query: str) -> str:
    """在用户的知识库里搜索。"""
    return await KNOWLEDGE_BASES[ctx.deps.kb].search(query, user=ctx.deps.user)


app = create_app(agent, auth=auth, run_context=run_context)
```

页面这一侧由扩展脚本通过 `lovia.provideContext(key, fn)` 提供；每次发送消息（或新建定时任务）
时都会读取一次 `fn()`：

```js
const kb = document.getElementById('acme-kb'); // composer_actions block 里的 <select>
window.lovia.provideContext('kb', () => kb.value);
```

`run_context` 每次运行调用一次，可以是同步或异步函数，返回值就是这次运行的 `ctx.deps`，自动
续跑的每一段共用它。它在写入这次运行的任何数据之前执行，所以抛出 `HTTPException` 会拒绝这次运行，
且不留下任何痕迹：聊天消息会收到这个错误，不会新建对话，中断的运行仍可恢复；定时任务的这次触发
记为一次失败的运行，然后按计划等下一次。请让它尽快返回：每条消息都要等它返回后才开始运行。
`trigger` 包含：

| 字段 | 说明 |
| --- | --- |
| `client` | 页面提供的上下文（`provideContext` 的各个 key）：UI 状态。**它由浏览器控制，绝不要据此判断身份或权限** |
| `request` | 发起这次运行的请求，`auth=` 已经执行过：身份在这里（`request.state`）。没有人发起的运行为 `None` |
| `source` | 运行的来源：`user`（一条消息，或重连时恢复的运行）、`schedule:<id>`、`subagent:<session>`、`subagent-report:<task>` |
| `resumed` | 是否是重连时从 checkpoint 恢复的运行 |
| `session_id`、`agent` | 所属对话，以及即将运行的 agent |

每次运行都有上下文，包括没有人发起的那些：

- **重启后恢复。** checkpoint 不保存上下文，所以服务端把这次运行的 `client` 存在它的
  checkpoint 指针旁边，运行结束时清掉；恢复时取回它，`request` 是重连的那个请求。
- **定时任务。** 定时任务相当于一条延后发送的消息：在定时任务对话框里创建的，保存创建那一刻
  页面的上下文；agent 用 `schedule_run` 创建的，保存它所在运行的上下文。每次触发都用它，
  `request=None`。
- **子 agent 任务及其汇报**使用所属对话最近一次的上下文，`request=None`。

`client` 编码成 JSON 后上限 64 KB。不设置 `run_context` 时，运行没有上下文（和 core 的
`Runner` 一样）；如果请求带了上下文，服务端会记一条警告日志。

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

聊天 Run（流式或阻塞式）都由服务端后台任务托管。SSE 连接断开后，Run 仍会继续执行，客户端
可以稍后重连；阻塞式 `/api/chat` 的客户端断开后，Run 同样会执行完毕并写入聊天。

- **用户取消**：把已完成 Turn 写入 Session，移除悬空 Tool 调用并清理 Checkpoint。
- **服务端关闭**：协作式取消 Run，但保留 Checkpoint，以便部署后重新连接并恢复。
- **容量**：由 `max_background_runs` 限制；满载时新请求返回 HTTP 429。
- **阻塞式 `/api/chat`**：等待 Run 结束并返回结果（[详见](http-api.md#阻塞式-turn)）。
  由于没有客户端可询问，需要审批的 Tool 调用交给 Agent 的 `approval_handler`，否则拒绝。

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
- 聊天页不设置 Content-Security-Policy，所以模板 block 和扩展脚本可以嵌入其他站点的 iframe。
  lovia 渲染的不可信内容（`html` 页面、文件面板的 HTML 预览）运行在自带防护的框架里：不透明源，
  也不能把自己导航到别处。你自己设置的页面策略（在反向代理或中间件里）会被这些 `srcdoc` 框架
  继承；页面依赖内联脚本，所以 `script-src` 必须允许内联；`frame-src` 不影响 `srcdoc` 框架。

生产使用前请阅读完整的[生产部署](deployment.md)指南。

## 延伸阅读

- [Web UI](web-ui.md)：内置浏览器体验与 CLI
- [HTTP API](http-api.md)：端点、SSE 格式和 `ChatStore`
- [工具审批](tools.md#工具审批)：服务端审批流程
