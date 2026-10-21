# 自定义视图（views）：在聊天之外加页面

> 状态：已实现（v0.9.106）· 2026-10-21 · 范围：`lovia/web`（不改 core）
>
> 行号指向 v0.9.104（`129d31d`）。这是 [web-customization.md](web-customization.md) 五层扩展方案的延续：那份方案覆盖聊天页内部的定制，本文补上“主区域显示别的内容”。
>
> 术语：本文的“视图”（view）指主区域里替代聊天显示的页面。它和“工具视图”（tool view，工具卡片的呈现方式）是两回事；和 `show_html` 产生的“页面”组件也不同。

## 结论

新增一个公开扩展点：部署方用 `lovia.registerView(id, view)` 注册视图，用普通链接 `<a href="?view=id">` 导航过去；lovia 负责路由、显示切换、侧栏导航项的选中态，以及视图和对话之间的切换。

```html
{% block sidebar_nav %}
<a href="?view=dashboard">…仪表盘</a>
{% endblock %}
```
```js
lovia.registerView('dashboard', {
  title: '仪表盘',
  render(el, ctx) { /* 画进 el；ctx.path 是 id 之后的子路径 */ return cleanup; },
});
```

`window.lovia` 增加 `registerView`、`openView`、`closeView`、`state.view` 和 `view` 事件；模板增加一个容器 `data-part="view"`。实现是新模块 `views.js`（约 230 行，含注释），加上其余 6 个文件里的少量接线。

## 1. 问题

### 1.1 需求

一位用户基于 lovia web 搭建测试平台，需要在侧栏放“工作台 / 测试设计 / 测试执行 / 流程编排 / 平台集成”等入口，点击后主区域换成对应内容，点对话再回到聊天。“Agent 应用 + 几个业务页面”的形态很常见：运营后台、数据分析、客服工作台都需要。

### 1.2 现在只能怎么做

[web-customization.md](web-customization.md) 的扩展点都针对聊天页内部：block 往固定位置放静态内容，`window.lovia` 注册卡片、工具视图和消息操作。主区域永远是聊天列。

测试平台示例用公开接口拼出了视图切换（约 60 行），但留下了几个只有 lovia 才能做对的问题：

| 问题 | 原因（行号 v0.9.104） |
| --- | --- |
| 隐藏聊天列要依赖内部结构 `main.page > .chat-main` | 聊天列没有 `data-part`，`.chat-main` 是内部类名（[index.html:103](../../lovia/web/templates/index.html#L103)） |
| 再点一次**当前**对话回不到聊天 | `switchSession` 遇到相同 id 直接返回，不触发任何事件（[sessions.js:733](../../lovia/web/static/js/sessions.js#L733)） |
| 侧栏收起后、或在手机上，打开视图就找不到展开侧栏的按钮 | 展开按钮和手机上的汉堡按钮都在聊天顶栏里（[index.html:105-108](../../lovia/web/templates/index.html#L105)），隐藏聊天列就一起藏掉了 |
| 在视图里按 Esc，会停掉看不见的运行 | Esc 在捕获阶段取消正在流式输出的运行（[main.js:238](../../lovia/web/static/js/main.js#L238)），它不知道聊天已被隐藏 |
| 视图打开期间对话在等审批或回答，用户看不到 | 审批卡片画在被隐藏的聊天列里，只有 lovia 知道这个状态（[chat.js:864](../../lovia/web/static/js/chat.js#L864)、[:1111](../../lovia/web/static/js/chat.js#L1111)） |
| 深链接和 lovia 的 `?session=` 各管各的 | lovia 用 `replaceState` 维护 `?session=`（[store.js:108](../../lovia/web/static/js/store.js#L108)），扩展只能另找地方（示例用了 `#tf=`） |
| 导航项要借用内部类名 `sidebar-nav-item` 才能和内置项长得一样 | 类名属于内部实现 |

判断标准：**部署方只用公开接口能不能把这件事做对**。上表前五项做不到，所以这是 lovia 该提供的扩展点，而不是十行就能写完的用户侧代码。

## 2. 方案对比

| 方案 | 评价 |
| --- | --- |
| A. 给聊天列加 `data-part="chat"`，路由仍由部署方写 | 只解决第一项；顶栏按钮、当前对话、Esc、审批提醒仍要部署方去碰内部状态 |
| B. 服务端多页面：`ChatUI(pages=[…])`，每页一个模板 | 每次切换整页刷新，侧栏的对话列表、事件流都要重建；侧栏脚本依赖聊天页的 DOM，不能在别的模板里复用 |
| C. 外层自建壳页面，用 iframe 嵌入 lovia | 两套侧栏，登录、主题、状态都要跨 iframe 同步；不是 lovia 的事 |
| **D. `registerView` + 普通链接，lovia 负责路由** | 部署方只写视图内容；上表七项全部由 lovia 处理 |

D 和 lovia 已有的扩展方式一致：`registerRenderer` 管“工具结果画成什么”，`registerToolView` 管“工具卡片怎么读”，`registerView` 管“主区域显示什么”。注册、渲染、清理的约定都沿用组件渲染器。

## 3. 设计

### 3.1 接口

```ts
interface View {
  /** 视图打开时顶栏显示的标题。 */
  title?: string;
  /**
   * 把视图画进 el。每次打开（或子路径变化）调用一次；可以是 async；
   * 返回的函数在视图关闭、被替换或重新渲染前调用。
   */
  render(el: HTMLElement, ctx: ViewContext): void | (() => void) | Promise<void | (() => void)>;
}
interface ViewContext {
  /** `?view=<id>/<path>` 里 id 之后的部分；没有时为 ''。 */
  readonly path: string;
}

interface LoviaApi {
  /** 注册视图 `id`，或替换已有的；null 移除。 */
  registerView(id: string, view: View | null): void;
  /** 打开视图：`'exec'`，或带子路径的 `'exec/perf'`。 */
  openView(target: string): void;
  /** 回到聊天（当前对话不变）。 */
  closeView(): void;
}
interface LoviaState { /** 打开的视图，如 'exec/perf'；在聊天时为 null。 */ readonly view: string | null; }
interface LoviaEvents { view: { view: string | null }; }
```

- **id**：`[a-z0-9][a-z0-9-]*`，`lovia` 开头的留给 lovia 自己；不合法时 `registerView` 抛 `TypeError`。
- **子路径**：`openView('exec/perf')` 打开视图 `exec`，`ctx.path === 'perf'`。用于视图内标签页、详情页的深链接；只占一个 URL 参数，不会和 lovia 的 `session`、`token` 参数冲突。
- **`openView` 指向未注册的 id**：抛 `TypeError`，这是调用方的代码错误。链接指向未注册的 id 的处理见 3.3。

### 3.2 视图与对话的关系：视图盖在对话上面

打开视图**不离开当前对话**：对话仍是 `state.sessionId`，正在进行的运行照常流式输出，URL 同时保留 `?session=` 和 `?view=`。关闭视图就回到原来的对话，输入框草稿还在。

这样视图可以用来“看一眼再回来”，和对话互不打断。下面这些**用户要看某个对话**的操作会关闭视图：

- `closeView()`；
- 点击任意对话，**包括当前对话**：`switchSession` 在“相同 id 提前返回”之前先关闭视图。它的调用方（对话列表、全部对话弹窗、后台任务列表、`lovia.openSession`、启动时恢复 URL 里的对话）都是这个语义；
- 开始新对话：“新对话”按钮、快捷键、`lovia.newChat()`。

`clearChat` 本身**不**关闭视图：它也会在切换 Agent、删除当前对话时被调用（[main.js:129](../../lovia/web/static/js/main.js#L129)、[sessions.js:725](../../lovia/web/static/js/sessions.js#L725)），用户在看视图时做这两件事，不该被带回聊天。同理，新对话首次保存时发出的 `session` 事件也不关闭视图。

`lovia.send()` 不关闭视图：消息照常发到当前对话。需要让用户看到对话的视图，自己先调用 `closeView()` 或 `newChat()`。

### 3.3 URL 与链接

- **URL**：`?view=<id>[/<path>]`，与 `?session=` 一样用 `replaceState` 维护，不新增历史记录，后退键的行为与今天一致。把对话和视图一起改成写入浏览历史，是以后可以单独做的改进。
- **链接即导航**：lovia 在 `document` 上拦截满足以下条件的 `<a>` 点击，改为 `openView`：
  - 普通左键点击，没有按修饰键，事件未被阻止；
  - 没有 `target`（或为 `_self`），没有 `download`；
  - 与当前页面同源、同路径，且带 `view` 参数；
  - 不在消息里（`[data-part="turn"]` 之外）。模型回复和工具卡片里的链接保持今天的处理方式，模型输出不能驱动导航。欢迎页、侧栏、顶栏、视图自身里的链接都会被拦截。

  中键、Ctrl/⌘ 点击、复制链接都按浏览器原生行为：新标签页打开后，启动流程读取 `?view=`。所以部署方在模板、视图、消息操作里写普通链接即可，不需要 JS。文档建议写相对形式 `?view=…`，这样在子路径部署下也成立。
- **未注册的 id**：链接指向未注册的视图时提示一次（`view.unknown`）并留在原处；启动时 URL 里的视图未注册（扩展加载失败或超时）同样提示一次，并从 URL 去掉 `view` 参数，停留在聊天。
- **启动**：在恢复 `?session=` 之后、`ready` 兑现之前打开 URL 里的视图。扩展在启动前加载（[extensions.js](../../lovia/web/static/js/extensions.js)），所以视图已注册。`ready` 的文档相应改为“……以及 URL 里的对话和视图都已加载完”。

### 3.4 显示

模板在聊天列里、顶栏之后放一个容器，顶栏里加一个视图标题：

```html
<header class="topbar" data-part="topbar">
  …<h1 id="chat-title" data-part="chat-title">…</h1>
  <h1 id="view-title" class="chat-title" hidden></h1>…
</header>
<section id="view" class="view" data-part="view" hidden></section>
<div id="chat-region" …>
```

视图容器是聊天列里的一个**覆盖层**（绝对定位，盖住顶栏以下的区域）。视图打开时，`<body>` 带上一个内部类：

- 对话区和输入框只设为 `visibility: hidden` 并加上 `inert`，**不用 `display: none`**。它们保持布局，所以在下面继续流式输出的运行照常滚动到底，关闭视图时对话停在应在的位置；`inert` 让它们拿不到焦点和点击。
- 文件面板，以及顶栏里只属于对话的元素（对话标题、后台任务、导出、文件按钮、运行进度）用 `display: none` 隐藏，它们不需要保持布局。

顶栏本身保留，显示视图标题。于是：

- **展开侧栏的按钮和手机上的汉堡按钮始终可见**，部署方在 `topbar_actions` 里放的应用级按钮也还在；
- 视图标题是单独的元素：对话标题在视图打开期间仍可能被更新（自动生成标题、重命名，[sessions.js:230](../../lovia/web/static/js/sessions.js#L230) 等），各写各的，互不覆盖。

容器是公开的 `data-part="view"`，部署方可以给它加样式。lovia 每次渲染时在容器里放一个新的子元素交给 `render`，部署方不会拿到上一个视图残留的 DOM。关闭视图时恢复原状。

文件面板在窄屏上会临时收起侧栏（[ui.js:118](../../lovia/web/static/js/ui.js#L118)）；视图打开时文件面板只是被隐藏，这个临时状态保持不变。展开按钮可见，用户随时可以展开。

### 3.5 生命周期

- 每次打开或子路径变化：先执行上一个视图的清理函数，再调用 `render`，然后才发出 `view` 事件。这样监听器在事件里打开另一个视图（比如重定向）时，新视图会替换旧视图，而不是被旧视图随后的渲染盖掉。
- 在手机上，每次显示视图都会收起侧栏抽屉，否则抽屉会盖住刚打开的视图。打开对话（包括新对话）时同样收起，修正了今天点对话后抽屉不收起的问题。
- `render` 抛错或返回的 promise 被拒绝：容器里显示“视图加载失败”（`view.failed`），错误打印到 console，页面其余部分不受影响。异步 `render` 完成前视图已被切走：结果丢弃，并立即调用它返回的清理函数（沿用组件渲染器的代数计数）。
- 用同一个 id 再次注册：如果该视图正打开，用新的 `render` 重新渲染；传 `null` 移除，如果正打开则关闭。
- **切换主题不重新渲染**：组件在切换主题时会重画（图表要换颜色），但视图通常带有表单、滚动位置等用户状态，重画会丢掉这些。视图需要响应主题时监听 `theme` 事件。切换语言会整页刷新（[settings.js:141](../../lovia/web/static/js/settings.js#L141)），不需要处理。
- 视图打开期间，Esc 不再取消运行（运行不在屏幕上）。

### 3.6 侧栏导航项

- **样式**：部署方放进 `sidebar_nav` block 的链接，lovia 按内置导航项的样式绘制，部署方不再需要借用内部类名 `sidebar-nav-item`。这是写进文档的样式约定，不新增类名或 `data-part`。
- **选中态**：只作用于 `[data-part="sidebar-nav"]` 里的链接——链接的视图 id（不含子路径）与当前视图相同时加 `aria-current="page"`，否则移除。只按 id 比较，是为了打开 `exec/perf` 时侧栏的“测试执行”仍显示选中；不处理侧栏外的链接，是因为视图内部的标签页需要按完整子路径区分选中，那属于视图自己的状态。

### 3.7 等待用户时的提醒

视图打开期间，当前对话里有工具进入 `awaiting`（审批、`ask_human` 提问），lovia 提示一次 `view.needsYou`（“对话在等你确认”），运行就不会在用户看不到的地方一直挂到 `approval_timeout`。提示不关闭视图，用户点当前对话即可回去处理。

已知局限：通过深链接同时打开对话和视图时，对话历史里已有的待审批项在视图打开之前就渲染完了，不会触发提示。

## 4. 明确不做

| 提议 | 原因 |
| --- | --- |
| 视图写入浏览历史（后退键回到上一个视图） | 对话切换今天也不写历史，单独给视图做会让两者行为不一致；以后两者一起改 |
| `layout: 'split'`（视图旁边常驻对话） | 有价值，但会改变聊天列的布局约定；等有具体需求再加，到时在 `View` 上加字段不是破坏性变更 |
| 视图改变 `document.title` | 标题已被“未读完成数”角标使用（[sessions.js:120](../../lovia/web/static/js/sessions.js#L120)），两者叠加需要额外设计；顶栏标题已足够 |
| 由 lovia 渲染导航项（`registerView` 时传 `icon`、`label`） | 导航放在哪、怎么分组是部署方的事，模板 block 已经能表达；链接加样式约定更简单，首屏不依赖 JS |
| 消息里的链接也能打开视图 | 会让模型输出驱动导航；有需求时由工具或消息操作显式调用 `openView` |
| `ViewContext` 里放 `signal`、`params` 等 | `render` 返回清理函数已覆盖中止的需要；子路径覆盖了深链接，第二个参数通道会和 lovia 自己的 URL 参数抢名字 |

## 5. 实施

一个 PR：

1. `static/js/views.js`：注册表、打开与关闭、URL、链接拦截、侧栏选中态、渲染生命周期（代数计数、错误提示）。
2. `index.html`：`#view` 容器与 `#view-title`；`styles.css`：视图打开时隐藏对话相关元素、侧栏导航链接样式、容器基础样式。
3. `sessions.js`：`switchSession` 在提前返回之前关闭视图；“新对话”按钮关闭视图。`main.js`：快捷键和 `lovia.newChat` 关闭视图；Esc 处理器在视图打开时不取消运行；启动时打开 URL 里的视图。`chat.js` / `tools.js`：工具进入 `awaiting` 时发出内部事件，视图打开期间提示。
4. `lovia.js` / `lovia.d.ts`：`registerView`、`openView`、`closeView`、`state.view`、`view` 事件；`ready` 的说明。约定测试固定新成员与事件名。
5. `i18n.js`：`view.failed`、`view.unknown`、`view.needsYou` 中英文案。
6. `lovia web init-ui` 生成的 `extension.js` 加一个注释掉的视图示例；`examples/33_web_extensions.py` 增加一个视图，演示链接导航和子路径。
7. 测试：`tests/web/js/views.test.mjs`（id 校验、子路径解析、URL 读写、链接拦截条件、侧栏选中态、生命周期与清理、渲染出错、晚到的异步结果被丢弃）；`tests/web/test_web.py` 的公开 `data-part` 列表加入 `view`；端到端（verify skill）：链接打开、深链接启动、点当前对话返回、运行期间打开视图再回来、侧栏收起时的展开按钮、视图里按 Esc 不停止运行、等待审批时的提示。
8. 文档：`web-server.md`（中英文）新增“视图”一节，`window.lovia` 表格与事件表补上新成员；README / README-zh 的 Web 定制段落提一句。

## 审查记录

初稿完成后逐条做了对抗性审查，以下问题已并入正文：

| # | 发现 | 处理 |
| --- | --- | --- |
| 1 | 初稿在 `clearChat` 里关闭视图，但切换 Agent、删除当前对话也会调用它，用户会被意外带回聊天 | 只在“用户要看某个对话”的入口关闭（3.2） |
| 2 | 视图打开时按 Esc 会取消看不见的运行（捕获阶段的全局处理器） | 视图打开期间 Esc 不取消运行（3.5），并列入端到端检查 |
| 3 | 视图标题若写进 `#chat-title`，会被后台生成的对话标题覆盖 | 单独的 `#view-title`（3.4） |
| 4 | 选中态按 id 作用于所有链接，会让视图内同一 id 的所有标签页都显示选中 | 只作用于侧栏导航里的链接（3.6） |
| 5 | 初稿把整个 `transcript` 排除在链接拦截之外，可欢迎页（`data-part="welcome"`）就在 transcript 里 | 改为只排除消息 `[data-part="turn"]`；补全拦截条件（修饰键、`target`、`download`）（3.3） |
| 6 | “视图”与“工具视图”、`show_html` 的“页面”术语冲突 | 文档统一称“视图（view）”并在开头说明区别；API 名保持 `registerView`，与 `registerToolView` 靠限定词区分 |
| 7 | 深链接同时带对话和视图时，已存在的待审批项不会触发提醒 | 记为已知局限（3.7），不为此增加机制 |
| 8 | 手机上打开视图后侧栏仍盖着内容 | 初稿推迟到 #271；第二轮审查后改为在本 PR 处理（见下） |
| 9 | 是否需要 `closeView`，还是用 `openSession(当前 id)` 代替 | 保留 `closeView`：对新对话（尚无 id）也成立，语义直接 |
| 10 | 过度设计检查：`title`、提醒、子路径是否必要 | 三者都对应 §1.2 的具体问题或深链接需求，保留；`signal`、`params`、导航项渲染、写历史均推迟（§4） |

实现完成后又做了一轮代码审查，以下问题已修复：

| # | 发现 | 处理 |
| --- | --- | --- |
| 11 | 视图打开期间流式输出的回复，回到聊天后停在中间（实测 scrollTop 707 / 6935）。原因是聊天区被 `display: none` 后，滚动到底的计算全部落空 | 聊天区改为 `visibility: hidden` + `inert`，视图做成覆盖层（§3.4）。修复后回来时停在底部（6928 / 6935）。这也一并消除了同类的布局测量问题 |
| 12 | 视图会让用户更频繁地通过侧栏导航，手机上抽屉不收起的问题被放大 | 显示视图、打开对话时都收起抽屉（§3.5） |
| 13 | 先发事件、后渲染：监听器在事件里同步打开另一个视图时，会被外层随后的渲染盖掉 | 改为先渲染、再发事件（§3.5），并加了单测 |
