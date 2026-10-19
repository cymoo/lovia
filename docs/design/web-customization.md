# Web UI 定制：在 lovia web 之上扩展

> 状态：实施中（第二版方案，按 §7 逐个 PR 落地）· 2026-10-19 · 范围：`lovia/web`；只有 §5.8 会改 core，列为可选
>
> 背景：已有用户想定制 lovia web 的界面，又不想从头写前端。目标是优雅、健壮、易扩展，对终端用户（UX）和开发者（DX）都友好。现阶段用户不多，必要时可以做破坏性变更。文中的行号指向 v0.9.90（05a3c07）。

## 结论

把"在 lovia web 之上扩展"整理成五层，每层只用一种机制。内置功能也走同一套公开接口实现，以此证明这些接口够用：

| 层 | 用户要做的事 | 机制 | 语言 |
| --- | --- | --- | --- |
| 配置 | 文案、品牌、语言、登录 | `ChatUI(...)` 字段 | Python |
| 主题 | 配色、字体、圆角，以及隐藏界面元素（包括某些工具卡片） | CSS token + `data-part` 约定 + `@layer` | CSS |
| 页面骨架 | 在固定位置插入静态内容（logo、链接、输入框旁的控件） | Jinja block | HTML |
| 前端扩展 | 自定义工具卡片、响应运行事件、调用页面动作，以及（后续）消息操作 | `window.lovia` v1 | JS |
| 数据通路 | 把 UI 状态或登录身份交给 agent 的工具 | `ChatRequest.context` → `create_app(run_context=)` | Python + JS |

分七个 PR 推进，见第 7 节。其中会推翻三条旧决定（理由见 §2.3）：

| 旧决定（web-extensibility.md §5） | 新方案 | 为什么变 |
| --- | --- | --- |
| 不做前端插件 API | 提供一个精简的 `window.lovia` v1 | 已有真实用户；内部模块的地址带版本 token，用户脚本没法稳定 import（P-1）。与其让用户去读内部实现，不如给一个小而稳定的接口 |
| 不加额外静态目录参数 | `ChatUI(assets=dir)` 替代 `templates=` | 现在的"模板目录 + `scripts=` + 用户自己 `app.mount(name=...)` + `url_for`"是同一件事被拆成了三个概念，合并后概念变少 |
| `chat.js` 等到有具体改动需要时再拆 | 现在拆 | 工具视图和事件接口必须改它的渲染路径，触发条件已经满足 |

## 1. 用户要定制什么

按改动深度从浅到深：

| 级别 | 典型诉求 | 现在能否做到 |
| --- | --- | --- |
| L0 配置 | 改欢迎语和示例问题、接入自己的登录、换 logo 和 favicon、默认用中文 | 前两项可以；logo、favicon、默认语言不行 |
| L1 主题 | 品牌色、字体、圆角；隐藏"导出"或"Agent 切换" | 配色可以（CSS token）；隐藏只能靠不稳定的 id |
| L2 骨架 | 侧栏加"帮助中心"入口、顶栏加按钮、输入框旁加"知识库"下拉 | 只能往 `sidebar_footer` 里插 |
| L3 行为 | 美化或隐藏 MCP 工具卡片、给回复加 👍/👎、埋点（发送、完成）、用代码发消息 | 只有 `ui_result` 渲染器；其余都得碰内部模块 |
| L4 数据 | 输入框旁的控件影响本轮工具行为；工具知道当前是谁在用 | 做不到 |
| L5 替换 | 完全自己写前端 | 可以：`build_api_router` + api.js + 带类型的 SSE 协议 |

L0、L1 和 L5 基本到位，缺口集中在 L2–L4。用户说的"在 lovia web 基础上扩展"，正好落在这三级。

## 2. 现状评估

### 2.1 值得保留的

- **组件注册表**（[components.js](../../lovia/web/static/js/components.js)）质量很高：
  - render 出错会被隔离，只显示一条提示；
  - 支持异步 render 和清理函数，用代数计数丢弃过期结果；
  - 先注册后注册都行，注册时会重绘已在屏幕上的组件；
  - 支持 HTML 导出快照。

  本文其它扩展点都复用这套挂载机制，不再另造一套。
- **`api.js`**：唯一的请求入口，统一处理 base 路径和 401。
- **SSE 协议**：用 TypedDict 定义，文档表格由测试校验（[sse.py](../../lovia/web/sse.py)）。
- **`ChatUI`、子路径部署、`auth=` / `login_url`、`lifespan=`**：都已经稳定。

### 2.2 问题（都已在代码里核对过）

**P-1　扩展脚本拿不到内部模块。** 静态资源挂在 `/static/<token>/` 下（[app.py:88](../../lovia/web/app.py#L88)、[:362](../../lovia/web/app.py#L362)），token 每个版本都会变。用户脚本只能用 `app_config.static_base` 拼出地址再动态 import，这既别扭又依赖内部实现。所以 `window.lovia` 实际上是唯一可行的扩展接口，而它目前只有 `registerRenderer` 和 `getRenderer` 两个函数。

**P-2　扩展脚本在启动之后才执行。** 模板里，`ChatUI.scripts` 排在 `main.js` 后面（[index.html:340](../../lovia/web/templates/index.html#L340)）。`main.js` 的启动代码在第一个 `await` 之前就已经执行了 `applyStaticI18n()` 和所有 `init*()`（[main.js:296](../../lovia/web/static/js/main.js#L296)）。扩展执行时，静态文案已经翻译完，各模块也已初始化；之后的历史渲染和扩展执行谁先谁后取决于网络，没有保证。

**P-3　工具的展示方式按工具名写死在 chat.js 里**，第三方工具享受不到同等待遇：
- `PATH_TOOLS` / `STALE_TOOLS`（[chat.js:776](../../lovia/web/static/js/chat.js#L776)）；
- 只给 shell 显示的描述副标题（[:809](../../lovia/web/static/js/chat.js#L809)）；
- read_file 的分页头拆分和代码高亮（[:915](../../lovia/web/static/js/chat.js#L915)）；
- todo 改为面板显示（[:1101](../../lovia/web/static/js/chat.js#L1101)）；
- ask_human 渲染成交互卡片（[:1346](../../lovia/web/static/js/chat.js#L1346)）。

结果是：MCP 工具既没法美化，也没法隐藏；面向终端用户的产品里，人人都会看到 `read_file {"path": …}` 这种原始调用。

**P-4　实时流和历史回放是两条渲染路径，都直接操作 DOM。**
- 实时流走 `handleEvent` 里的 switch（[:2453](../../lovia/web/static/js/chat.js#L2453)），历史回放走 `renderHistoryWindow`（[:2119](../../lovia/web/static/js/chat.js#L2119)），二者都直接拼 DOM。
- 组件之外没有任何数据模型，扩展只能去碰 DOM。
- 两条路径的分歧是反复出现的 bug 来源。代码注释里记着两处：
  - 回放时按并不存在的 `type` 字段匹配，导致工具结果全部配不上、被隐藏；
  - 恢复的运行会重新宣告已回放过的调用，导致多出一张永远等不到结果的重复卡片。

**P-5　公开的样式约定只有 token。** 类名和 id 都声明为内部实现（[web-server.md:132](../en/web-server.md)），"隐藏导出按钮"这种最常见的需求也没有正式支持的写法。要压过内置样式，用户只能写更长的选择器或者加 `!important`。

**P-6　i18n 的做法比较脆弱。**
- 静态文案靠一张"选择器 → 文案 key"的表，在启动时重新贴一遍（[i18n.js:942](../../lovia/web/static/js/i18n.js#L942)）。用户覆盖模板时只要挪动或删掉某个元素，对应的翻译就会悄悄失效。
- 不能覆盖文案，也不能新增语言；默认语言只能跟随浏览器。
- 用户自己在模板里加的内容没法接入 i18n。

**P-7　UI 状态传不到 agent。** `ChatRequest` 只有 `message`、`session_id`、`agent`、`attachments` 四个字段（[schemas.py:150](../../lovia/web/schemas.py#L150)）。web 层所有 `Runner.run/stream` 调用都没有传 `context=`（例如 [chat.py:101](../../lovia/web/api/chat.py#L101)），所以工具的 `RunContext.context` 永远是 `None`。

**P-8　品牌元素写死，欢迎页由两份代码各画一遍。**
- favicon 和 theme-color 写死在模板里（[index.html:6](../../lovia/web/templates/index.html#L6)），侧栏标题只是 `{{ title }}` 文本（[:52](../../lovia/web/templates/index.html#L52)）。
- 欢迎页有两份实现：首次进入时由 Jinja 渲染；点"新对话"后由 `renderEmptyState()` 用 JS 重画一遍（[chat.js:2387](../../lovia/web/static/js/chat.js#L2387)、[:3172](../../lovia/web/static/js/chat.js#L3172)）。所以模板里加给欢迎页的任何内容，一点"新对话"就消失。

**P-9　定制用到的文件分散在三个概念里。**
- 模板目录用 `templates=`；
- 脚本用 `scripts=`，而且要用户自己 `app.mount("/ext", StaticFiles(...))`；
- 样式要先 `app.mount(..., name="brand")`，再在模板里写 `url_for('brand', ...)`。

三者讲的是同一件事——"我的 UI 文件"。

**P-10（顺带发现）　定时任务无法关闭。** `features.scheduling` 写死为 `True`（[api/\_\_init\_\_.py:66](../../lovia/web/api/__init__.py#L66)），schedules 路由总是挂载。嵌入方的产品里因此总会出现一个"定时任务"入口，有权调用 API 的用户都能创建无人值守、周期执行的运行，而部署方没有开关可以关掉。

**P-11　消息没有可长期引用的标识。** `MessageOut` 没有 id（[schemas.py:215](../../lovia/web/schemas.py#L215)）。唯一现成的定位方式是 rewind 用的 `user_turn`，即按顺序数的第几个用户回合（[schemas.py:367](../../lovia/web/schemas.py#L367)）。它只适合"立即执行"的操作，不能当长期引用：
- **rewind 后会复用序号。** 用户点了 👎 再点"重新生成"，新回复的序号和旧回复相同，存下来的 👎 就落到了新回复上。这恰恰是反馈功能最常见的用法。
- **多个标签页之间会错位。** 另一个标签页 rewind 之后，本标签页的序号就过期了。rewind 本身今天也有这个竞态。
- **将来做分支编辑（#150）时，序号不再唯一。**
- **序号由客户端在实时和回放两条路径上各算一遍**，有分歧的风险（同 P-4）。

### 2.3 为什么推翻旧决定

旧决定成立的前提是"没有外部前端用户，`body_end` 加 api.js 就够了"。现在前提变了：

1. 有了真实用户，他们会直接 import 内部模块（P-1 说明这条路既别扭又脆弱）。这反而让内部实现变成了事实上的接口。一个经过筛选的门面，正是把内部实现藏起来的标准办法。
2. 新接口由内置功能先用一遍：charts、tables、html 已经在用 `registerRenderer`；本方案再让 shell、read_file 的卡片走 `registerToolView`，让"复制"按钮走消息操作接口。这样接口是否够用，由内置功能直接验证。

## 3. 同类框架怎么做

| 框架 | 主题 | 骨架和行为扩展 | 工具或自定义 UI | 文案 | 稳定性承诺 |
| --- | --- | --- | --- | --- | --- |
| Chainlit | `public/theme.json` 写 CSS 变量；`custom_css` | 配置 logo 和 favicon | `CustomElement`：JSX + props，提供 `updateElement` / `callAction` / `sendUserMessage` | 翻译文件 | 文档说明没有 CSS 类名清单，要自己用开发者工具查 |
| Gradio | `theme` / `css` / `js` / `head` 参数（6.x 移到 `launch()`） | `js` 参数加 `head` 注入 | 自定义组件（需要构建） | — | 明确表示查询选择器不保证跨版本可用 |
| LibreChat | — | `librechat.yaml` 的 `interface` 段里有一组功能开关 | — | `customWelcome` 支持 `{{user.name}}` | 配置即约定 |
| assistant-ui | Tailwind | React primitives | 按工具名注册 UI，未注册的工具走 `ToolFallback` | — | 有 npm 版本号 |
| AI SDK UI | — | `useChat` | 带类型的 `message.parts`，包括 tool parts；data parts 分持久（可按 id 更新）和瞬时两种 | — | 有 npm 版本号 |
| AG-UI | — | 事件协议：`CUSTOM`、`STATE_SNAPSHOT/DELTA` | 工具调用以 START/ARGS/END 事件流式传输 | — | 协议规范 |
| Open WebUI | — | Action：消息工具栏按钮，在服务端执行 | Filter：输入输出中间件 | — | — |

对 lovia 的启示：

1. **用 CSS 变量换主题是业界共识，类名不承诺稳定也是共识。** lovia 可以再进一步，用 `data-part` 给出一份小而稳定的约定（§5.3）。
2. **"按工具名注册 UI，未注册的走兜底视图"是主流做法**（assistant-ui）。对应 `registerToolView(name | '*')`。
3. **扩展拿到的应该是数据，而不是 DOM**（AI SDK 的 parts）。对应工具状态模型（§5.5）。
4. **服务端驱动组件这一项，lovia 已经和 Chainlit 持平**：`ui_result` ≈ `CustomElement`，`ctx.send` ≈ `sendUserMessage`。这部分不用改。
5. **消息级操作很常见**（Open WebUI 的 Action），最典型的用途是 👍/👎 反馈。前提是消息有稳定的标识（§5.6）。
6. **文案用数据文件提供**（Chainlit），**功能开关放在配置里**（LibreChat）。
7. **流式自定义数据**（AG-UI 的 `CUSTOM`、AI SDK 的 data parts）属于协议层能力，对应 §5.8 的可选项。

## 4. 设计原则

1. **一层一种机制。** 配置归 `ChatUI`，样式和隐藏归 CSS，静态插入归 block，动态行为归 `window.lovia`，数据归请求和 `run_context`。不让两种机制争同一件事。
2. **内置功能先用公开接口。** 新接口的第一个使用者是 lovia 自己。
3. **约定要明确、可测试、带版本。** 公开接口包括：`window.lovia` 的成员和事件名、`data-part` 名、block 名、CSS token 名、i18n key。每一项都由测试固定，类型写在 `lovia.d.ts` 里。其它一切都是内部实现。
4. **能力在服务端开关，呈现在客户端调整。** 关掉定时任务要在服务端生效，不能只是把按钮藏起来；隐藏导出按钮则是纯呈现问题，用 CSS 解决。
5. **标识由服务端生成，客户端只回传。** 客户端不自己计算任何需要长期引用的 id。
6. **扩展出错不能拖垮页面。** 加载失败、加载卡住、render 抛错、回调抛错，都只影响那个扩展本身。
7. **零构建不变。** 继续使用原生 ES module 和 checkJs，不引入 npm 或打包。

## 5. 方案

### 5.1 配置层：`ChatUI`

```python
@dataclass(frozen=True)
class ChatUI:
    # 不变
    empty_title: str = "Where shall we begin?"
    empty_description: str | Sequence[str] = "..."
    empty_examples: Sequence[str] = ()
    login_url: str | None = None
    # 新增
    assets: str | Path | None = None       # 你的 UI 目录，替代 templates=
    scripts: Sequence[str] = ()            # 相对路径在 assets 里解析
    styles: Sequence[str] = ()             # 在内置 styles.css 之后加载
    logo: str | None = None                # 侧栏头部；为空时显示 title 文本
    favicon: str | None = None
    default_lang: str | None = None        # 用户还没选语言时的默认值；None 表示跟随浏览器
    messages: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
```

**`assets`：**
- lovia 把这个目录挂在 `{base}/ui/` 下，响应头设为 `Cache-Control: no-cache` 加 ETag。浏览器每次都会验证文件是否变化，没变化就返回 304。
  - 内置资源用的是"版本 token + 永久缓存"，这里不照搬：token 只在启动时计算一次，开发时每改一个文件都得重启服务才能生效。
  - 用户文件通常只有几个，每次多一次验证请求，代价可以忽略。
- 目录里有 `index.html` 时，就用它覆盖内置模板（取代 `templates=`）。模板文件（`*.html`）只给 Jinja 用，不对外提供下载。
- `scripts`、`styles`、`logo`、`favicon` 里的相对路径都在这个目录里解析；绝对 URL 原样使用。
- 用户不再需要 `app.mount`、`name=` 和 `url_for`。
- 目录不存在时报 `UserError`，并附带 hint。

**`logo`：** 只接收一张图。深浅主题要用不同 logo 的部署方，可以在 `sidebar_header` block 里放两张图，再用 `[data-theme]` 控制显示哪一张，`ChatUI` 不必为此再加字段。

**`messages`：**
- 合并进内置字典，例如 `{"zh": {"composer.placeholder": "问点什么…"}, "ja": {...}}`。
- 出现新的语言 key 就相当于新增一门语言，设置面板的语言列表随之增加。
- 必须作为服务端数据、经 `app_config` 下发，而不能交给扩展脚本注册：静态文案在启动时就要翻译好，扩展脚本赶不上（P-2）。
- 这意味着 i18n key 也成了公开约定。启动时会检查覆盖用的 key：内置字典里不存在的，在 console 里警告一次。这样拼错了，或者 lovia 升级后改了名，部署方都能马上发现，而不是悄悄失效。

**`default_lang`：**
- 只在用户没有选过语言时生效，用户在设置里的选择优先。
- 设置面板里的"自动"选项，语义随之变为"跟随部署默认（部署方没设时跟随浏览器）"。
- 名字里带 `default`，一看就知道不是强制锁定。
- 确实想锁定的部署方，用 `data-part` 把设置里的语言选择器隐藏掉即可，不必再加一种模式。
- 已知限制：语言选择器被隐藏之前就保存过语言偏好的用户，会继续沿用那个旧选择。

**欢迎页只保留一份实现**（解决 P-8 的第二点）：
- 服务端把整个欢迎页（标题、描述、示例问题、`welcome` block）渲染进一个 `<template>`。
- 首次进入和点"新对话"时，都由 JS 克隆这个模板。
- `app_config` 里的 `empty_*` 字段和 `store.emptyTitle` 等随之删除。
- CLI 未配置模型时显示的引导页保持现状。

**破坏性变更：** 删除 `templates=`，迁移方法是改名为 `assets=`。

### 5.2 主题层

- **把内置 CSS 包进 `@layer lovia { ... }`。** 不在任何 layer 里的用户样式天然优先，不管内置选择器的优先级多高，用户都不需要 `!important`。cascade layers 自 2022 年起已被主流浏览器全部支持。

  反过来，layer 里带 `!important` 的规则会压过 layer 外带 `!important` 的规则。内置 CSS 里只有 6 处 `!important`：`.hidden`、手机布局下隐藏展开按钮，以及 reduced-motion。它们本来就应该总是生效，所以这个副作用正好合适。
- **token 参考表写进文档**，由测试对照 `styles.css` 顶部，防止文档和实现脱节。
- **文档提醒**：想让深浅主题用不同的值，要分别写在 `[data-theme="light"]` 和 `[data-theme="dark"]` 下。直接写在 `:root` 上的值，两个主题都会被覆盖（今天也是这样，不是本方案引入的问题）。
- **`styles=`** 在内置样式之后加载。

### 5.3 页面骨架层：block 与 `data-part`

**block**：`head`、`sidebar_footer`、`body_end` 保留，新增五个：

| block | 位置 | 典型用途 |
| --- | --- | --- |
| `sidebar_header` | 包住侧栏标题 | 换成自己的品牌区 |
| `sidebar_nav` | 侧栏导航末尾 | "帮助中心""知识库"入口 |
| `topbar_actions` | 顶栏按钮区的最左侧 | 自定义按钮 |
| `composer_actions` | 输入框发送键旁 | 模式切换、知识库下拉 |
| `welcome` | 欢迎页示例问题之后，随欢迎页模板一起克隆（§5.1） | 公告、使用说明 |

规则：block 只能放在不会被 JS 重画的区域，否则内容会被冲掉；`welcome` 是唯一的例外，靠 §5.1 的模板克隆来保留。测试会检查每个 block 的内容在"新对话"、切换会话之后都还在。

**`data-part`**：标注在已有元素上（不新增包裹元素），作为公开的样式和定位约定：

```
侧栏    sidebar · sidebar-header · agent-switcher · sidebar-nav · new-chat · memory · schedules
        settings · sessions-label · sessions-search · sessions
顶栏    topbar · chat-title · topbar-actions · tasks · export · files-toggle
对话区  transcript · welcome · todo-panel · turn[data-role=user|assistant] · tool[data-tool=<name>]
输入框  composer · attach · context-ring
其他    files-panel · language（设置面板里的语言那一行）
```

`tool` 的 `data-status=running|awaiting|done|error` 随 PR 3 的工具状态模型一起加。

于是常见需求都只要一行 CSS：

```css
[data-part="export"] { display: none; }
[data-part="tool"][data-tool="read_process_output"] { display: none; }
```

**隐藏工具卡片只有这一种方式**，`ToolView` 不另设 `hidden`（原则 1）。审批卡片是独立的节点，不受工具卡片隐藏的影响，所以需要用户批准的调用永远不会被意外藏掉。

id 和类名仍是内部实现。block 名和 `data-part` 名由测试固定，改名就算破坏性变更。

**i18n 改用属性声明。** 去掉选择器表，模板元素自己写 `data-i18n="nav.newChat"`（另有 `data-i18n-title`、`data-i18n-placeholder`、`data-i18n-aria`）。用户模板里的内容也能用同一机制，再配合 `ChatUI(messages=)` 提供翻译。顺带修掉 P-6 里"挪动元素后翻译悄悄失效"的问题。

### 5.4 前端扩展接口：`window.lovia` v1

**加载时序**（解决 P-2）：
- `main.js` 一开始就创建 `window.lovia`，然后并行 import 所有扩展，全部加载完再启动应用。
- 等待最多 5 秒。超时的扩展不再阻塞启动，等它加载完再注册（注册时会重绘已有内容，所以晚到也能生效）。这样一个挂掉的 CDN 不会让整个页面白屏。
- 某个扩展加载失败时：在 console 打印错误，弹一次提示"扩展 X 加载失败"，页面照常启动。
- 扩展是部署方自己的代码，受信任，拥有页面的全部权限。文档建议把第三方脚本放进 `assets` 自托管，不要直接引用外部 CDN。

**接口**（类型随包发布为 `lovia.d.ts`）：

```ts
interface Lovia {
  version: string;                       // 包版本，用来做特性检测
  ready: Promise<void>;                  // 会话列表和 URL 指定的会话都已渲染完
  state: Readonly<{ sessionId: string | null; agent: string | null;
                    theme: 'light' | 'dark'; lang: string; busy: boolean }>;
  on(event: 'session' | 'agent' | 'run-start' | 'run-end' | 'theme',
     fn: (payload) => void): () => void; // 返回取消订阅函数

  // 渲染注册表，共用 components.js 的挂载机制
  registerRenderer(kind, { render, snapshot? }): void;     // 已有
  getRenderer(kind): Renderer | undefined;                 // 已有
  registerToolView(name | '*', view: ToolView): void;      // 新增
  registerMessageAction(id, action: MessageAction): void;  // 新增，在 PR 6（§5.6）

  // 页面动作
  send(text: string): void;        // 部署方代码受信任，不要求用户手势
  setDraft(text: string): void;
  newChat(): void;
  openSession(id: string): Promise<void>;
  toast(text: string, opts?: { type?: 'info' | 'error' }): void;

  // 工具
  api: typeof import('./api.js').api;   // 已经配好 base 路径和 401 处理
  t(key: string, params?): string;
  provideContext(key: string, fn: () => JsonValue): void;  // 在 PR 5（§5.7）
}
```

**为什么 `lovia.send` 不要求用户手势：** 组件里的 `ctx.send` 要求手势，因为组件内容可能是模型生成的，不可信。扩展是部署方写的代码，本来就能直接调 API。对它加同样的限制只会碍事，比如"从深链接 `?q=` 自动发送"就做不成。

**事件**只开放 5 个，都只针对当前打开的对话。它们由一个转换层从内部 store 事件映射而来，内部事件名照样可以随意改：

| 事件 | payload |
| --- | --- |
| `session` | `{ sessionId }`，新对话时为 `null` |
| `agent` | `{ agent }` |
| `run-start` | `{ sessionId }` |
| `run-end` | `{ sessionId, status: 'done' \| 'error' \| 'cancelled', usage }` |
| `theme` | `{ theme }` |

**`ToolView`**（解决 P-3）：

```ts
interface ToolView {
  summary?(tool: ToolState): string | null;       // 替代 "name + 参数" 摘要
  render?(el, tool: ToolState, ctx): void | Cleanup | Promise<...>;  // 卡片主体
  snapshot?(tool: ToolState, ctx): string;        // 用于 HTML 导出
}
interface ToolState { id: string; name: string;
                      args: unknown | undefined;  // 参数超过 256 KB 时为 undefined（沿用现有上限）
                      result?: string; isError: boolean;
                      status: 'running' | 'awaiting' | 'done' | 'error' }
```

- **优先级**：`ui_result` 信封 > 按名字注册的视图 > `'*'` 兜底视图 > 内置默认卡片。每个字段单独回退，例如只提供了 `summary`，卡片主体仍用默认渲染。
- **典型用法**：面向终端用户的产品写 `registerToolView('*', { summary: t => friendly(t) })`，就能把所有工具卡片换成友好的一句话。
- **render 的调用时机**：每次 `ToolState` 变化（例如从 running 变为 done）都会重新调用，复用组件注册表的代数计数和清理机制。
- **内置工具先用**：shell（用 `summary` 显示描述）、read_file（用 `render` 拆分页头并高亮）都改成通过 `registerToolView` 注册。
- **todo 和 ask_human 在 v1 保持特殊处理**：它们画在工具卡片之外（计划面板、交互卡片），硬塞进 ToolView 反而会让接口变复杂。

**稳定性保障：**
- `tests/web/js/contract.test.mjs` 固定 `window.lovia` 的全部成员和事件名；
- `lovia.d.ts` 由 tsc 检查，确保和实现一致；
- 文档写明 v1 之外的一切都是内部实现。

### 5.5 内部重构：拆分 chat.js，为工具建立状态模型

这是 §5.4 的前置工作，纯重构，行为不变：

1. **工具状态模型。** 为每个 call id 维护一个 `ToolState`。实时事件（`tool_call` / `approval_required` / `tool_result`）和历史回放都只负责更新它，再统一交给 `renderTool(state)` 渲染，P-4 里的两条路径在工具这一层合为一条。
   - 只为工具建模，不为整条消息建完整的 parts 模型：扩展的需求集中在工具上，而文本流式渲染依赖 `stream-blocks.js` 的增量 DOM，强行纳入统一模型代价大、收益小。
   - 以后真有消息级的扩展需求，再把模型扩展到其它 part。
2. **拆分模块。** 按即将暴露的接口拆：

   | 模块 | 职责 |
   | --- | --- |
   | `stream.js` | 消费 SSE，产出 `run-start` / `run-end` |
   | `tools.js` | ToolState、工具视图注册表、工具卡片 |
   | `history.js` | 历史回放 |
   | `composer.js` | 发送、草稿、附件、`provideContext` |
   | `turns.js` | 气泡、复制、（后续的）消息操作 |
   | `chat.js` | 只负责组装以上模块 |

**验证：**
- 现有的 pytest 和 node 测试全部通过；
- 用 verify skill 跑端到端：实时流、回放、断线重连、审批、ask_human、组件、HTML 导出。

### 5.6 消息标识与消息操作（会改服务端协议，单独一个 PR）

P-11 说明了 `user_turn` 只能用来定位"现在屏幕上的这条"，不能作为长期引用。可长期引用的标识要由服务端生成：

**`ref` = 所在运行分段的 `run_id` + 该用户回合在分段内的序号**，对外是一个不透明的字符串。

用户消息带 `ref`；对它的回复（可能跨多步工具调用，甚至跨多段自动续跑）沿用同一个 `ref`，以 `role` 区分。

**为什么它不会被复用：**
- Session 按运行分段存储，每段以 `run_id` 为键（[session.py:38](../../lovia/session.py#L38)）；对同一个 `run_id` 重复追加是空操作。
- rewind 只会截掉某一段的尾部，或删掉后面的整段（[stores/session.py:214](../../lovia/stores/session.py#L214)），从不往已有的段里追加。之后的新内容一定落进带新 `run_id` 的新段。
- 所以一个 `ref` 一旦被截掉，就永远不会再出现：
  - "👎 后重新生成"时，新回复拿到的是新 `ref`，旧反馈不会张冠李戴；
  - 多个标签页之间也不会错位；
  - 将来做分支编辑（#150）时，`ref` 依然唯一。

**其他情况：**

| 情况 | 结果 |
| --- | --- |
| 还在进行中的运行 | 一段运行写入 Session 之后，它的 `ref` 才确定，在此之前 `MessageRef.ref` 为 `null`。正在生成的回复本来也不该被评价。<br>不提前下发的原因：没有 checkpoint 时，`run_id` 要到写入 Session 时才由存储生成（core 不接受不带 checkpointer 的 `run_id`）。有 checkpoint 时虽然能提前知道，但为了规则统一也不提前 |
| 未成功结束的运行 | 没有 `done` 事件；下次加载历史时从分段拿到 `ref` |
| 自带 Session 且没有 checkpointer | 运行结束时 web 不知道存储生成的 `run_id`（`RunResult` 不带它），`done` 里就没有 `ref`，要等下次加载历史时才拿到。两个内置的 `ChatStore` 都带 checkpointer（[store.py:405](../../lovia/web/store.py#L405)、[:415](../../lovia/web/store.py#L415)），`run_id` 在运行开始前就已知，不受影响 |
| 上下文压缩 | 只影响发给模型的视图，不改 transcript，`ref` 不变 |
| 自定义的 Session | `segments()` 是协议里的读取接口，一定能拿到 `run_id` |

**协议改动（只在 web 层，不改 core）：**

| 位置 | 改动 |
| --- | --- |
| `MessageOut` | 用户消息带上 `ref`（历史和 `snapshot` 都由服务端按分段计算） |
| `done` 事件 | 带上本段运行各用户回合的 `ref`（自动续跑时每段都有自己的 `done`） |
| `RewindRequest` | 可以改为接收 `ref`，顺带消除 rewind 在多个标签页下的错位竞态 |

**消息操作：**

```ts
interface MessageAction { label: string; icon?: string;
                          when?(msg: MessageRef): boolean; run(msg: MessageRef, ctx): void }
interface MessageRef { sessionId: string; ref: string | null; role: 'user' | 'assistant'; text: string }
```

- 内置的"复制"按钮先改用这个接口。它不需要 `ref`，所以生成中途也能用。
- 需要长期引用的操作（比如反馈）写 `when: m => m.ref !== null`，在 `ref` 确定之前不显示。
- 反馈数据存在哪由用户决定：在 action 里调用自己的路由，以 `(sessionId, ref)` 为键保存即可；lovia 不负责保存反馈（这是十行以内就能写完的用户侧代码）。

### 5.7 数据通路：UI 状态 → RunContext（需先单独设计）

**这里的"状态"指什么：** 前端扩展在发送时提供的一个 JSON 对象，比如输入框旁"知识库"下拉框的当前选择 `{"kb": "product-docs"}`。下面称它为 client context。它：
- 不是 `run_context` 构造出来的 `RunContext.context` 对象——那个对象可能持有数据库连接之类的东西，不能序列化；
- 不是登录身份——身份每次都从请求里重新取得；
- 也不是滚动位置、主题这类纯 UI 状态。

**请求：**
- `ChatRequest.context: dict[str, JsonValue] | None`，上限 64 KB。
- 前端由 `lovia.provideContext(key, fn)` 在发送时收集，按 key 分区，避免多个扩展互相覆盖。某个 `fn` 抛错时，只跳过它那个 key，并在 console 报错。

**服务端：**
- 新增 `create_app(run_context=fn)`，`fn(trigger) -> Any | Awaitable[Any]`，返回值作为 `Runner.run/stream(context=...)` 传入。
- `trigger` 包含：

  | 字段 | 含义 |
  | --- | --- |
  | `request` | 没有请求的运行（定时任务、子 agent、汇报）为 `None`；恢复时是重连的那个请求 |
  | `session_id` | 会话 id |
  | `agent` | 本次运行的 agent |
  | `client` | client context |
  | `source` | 运行来源，直接沿用 supervisor 已有的取值：`chat` / `schedule:<id>` / `subagent:<sid>` / `subagent-report:<id>` |
  | `resumed` | 是否是从 checkpoint 恢复 |

- 不传 `run_context` 时，行为和现在一样（没有 context），不擅自加默认值。
- 如果请求带了 `context`，服务端却没配 `run_context`，就在日志里警告一次。这通常是部署时漏接了，悄悄忽略会很难排查。

**client context 的持久化：**

只有一个场景需要持久化：进程重启后，从 checkpoint 恢复一个中断的运行。checkpoint 不保存 `RunContext.context`，要求调用方在恢复时重新提供（[checkpointer.py:7](../../lovia/checkpointer.py#L7)）。如果不持久化，恢复后 `client` 就是空的，工具会悄悄换成默认行为（比如去搜错误的知识库）。同一进程内的运行、包括自动续跑的后续段，都直接用内存里的值，不需要读库。

- **推荐：存在 `chat_sessions` 行上，紧挨着 `active_run_id`，新增 `active_context_json`；两列同时写入、同时清除。**
  - 恢复时本来就是靠这一行的 `active_run_id` 找到正在进行的运行（[chat.py:271](../../lovia/web/api/chat.py#L271)），在同一处取 context 最直接。
  - `active_run_id` 本身就是"当前运行"的状态，只是恰好存在会话行上；新列与它同生同灭，并没有把"运行"的概念泄漏给"对话"。
  - 运行结束即清除，client context 不被长期保存，对隐私更友好。
- **不放进 `chat_runs`**（上一版的推荐，已撤回）：一条 `chat_runs` 记录可以跨多段自动续跑，它的 id 取自第一段的 run id（[supervisor.py:502](../../lovia/web/supervisor.py#L502)），而恢复的可能是后面某一段，还得另做映射。另外它会被长期保存，没有这个必要。
- **不放进 checkpoint**：checkpoint 属于 core，放进去会把 web 的概念泄漏到 core。

**定时任务：** `chat_schedules` 已经保存了 `input`，再并列存一列 `context_json`。"定时任务就是一个延后执行的聊天请求"这个模型依然成立。

**后台子 agent（待设计）：** web 模式下，子 agent 任务和"把结果汇报回父对话"这两种运行，都是独立的受管运行（[web/subagents.py:100](../../lovia/web/subagents.py#L100)、[:170](../../lovia/web/subagents.py#L170)），也需要 client context。
- 推荐：继承发起它的那次运行的 client context，与子任务一起保存。
- 不能临时去父对话里取：汇报送达时父运行可能早已结束，`active_context_json` 已被清除。

**信任边界：** client context 可以被客户端伪造。文档要写明：身份只能取自 `request.state`（由 `auth` 依赖写入），绝不能取自 client context。

**与 #221 的关系：** 这一项只是把身份传给工具，不做任何隔离。

### 5.8 可选：工具进度事件（会改 core，有需求再做）

工具调用 `ctx.progress(text, data=None)`，产生 `ToolCallProgress` 事件；web 层转成 SSE 的 `tool_progress`，更新 `ToolState.progress`，工具视图随之重绘。CLI 也可以把它打印出来。

这对应 AG-UI 的 `CUSTOM` 事件和 AI SDK 的瞬时 data parts。它会给 core 增加一个概念，需要单独设计，只有出现真实需求（例如"正在搜索 3/10"）时才做。

### 5.9 顺带：定时任务开关（P-10）

新增 `create_app(scheduling: bool)`，关闭时不挂载 schedules 路由、不启动调度器，`features.scheduling` 随之变为 false。

推荐 `create_app` 默认关闭，`lovia web` 显式传 `True`。理由：
- core 本身没有调度器，定时任务是 web 层额外加的能力；
- 嵌入方的产品里会凭空多出一个"定时任务"入口；
- 无人值守的运行如果遇到需要审批的工具，会一直挂起并占用一个后台运行名额（`approval_timeout` 的文档已经提醒过这一点）。

这是破坏性变更：在用定时任务的嵌入方需要加上 `scheduling=True`。

反方的理由也要说清楚：只要没人创建定时任务，开着就不花钱。

关闭后，数据库里已有的定时任务不会触发。实现时要确认：重新开启后，错过的触发时间会怎么处理。

## 6. 明确不做的

| 提议 | 不做的原因 |
| --- | --- |
| 发 npm 包、用 React/Vue 重写、引入构建步骤 | 违背零构建和依赖自带的设计；想要 React 的用户走 L5 |
| 改写成 Web Components（`<lovia-chat>`） | 等于重写一遍；嵌入别的页面用 iframe 加子路径部署就够了，"只要聊天区"的布局用 `data-part` 加 CSS 实现 |
| 支持完整替换模板 | 只支持"继承内置模板 + 填 block"；整页替换会和 JS 依赖的 65 个 DOM id 冲突 |
| 承诺类名和 DOM 结构稳定 | 公开约定只有 `data-part`、block、token、i18n key 和 `window.lovia` |
| 在 `ChatUI` 里为每个界面元素加显示开关 | 呈现问题用 `data-part` 加 CSS 解决；只有服务端能力才加开关（§5.9） |
| `ToolView.hidden` | 与 CSS 隐藏重复，违反"一层一种机制" |
| 用静态目录覆盖内置文件（替换 chat.js、styles.css） | 等于半个 fork，升级必坏 |
| 前端插件管理（启用/禁用、依赖、市场） | v1 只是一组注册函数，扩展就是普通的 ES module |
| JS 层的 slot 注册接口 | 页面级的插入用 block（服务端渲染、首屏不闪）；按条目的扩展用注册表。两种机制各管一件事 |
| 由 lovia 保存反馈数据 | 用户侧几行代码：消息操作加自己的路由 |
| 给 core 的 TranscriptEntry 加 id | `run_id` + 分段内序号已经足够稳定，不需要改持久化格式 |

## 7. 分阶段实施

| PR | 内容 | 破坏性 | 规模 |
| --- | --- | --- | --- |
| 1 主题与骨架约定 | `@layer lovia`；`data-part`；新增 5 个 block；欢迎页只保留模板一份实现；改用 `data-i18n` 属性；`ChatUI(default_lang, messages)`；约定测试 | `app_config` 删掉 `empty_*`（内部字段） | 中 |
| 2 扩展加载 | `ChatUI(assets=)` 替代 `templates=`；`styles=`、`logo`、`favicon`（相对路径都在 `assets` 里解析，所以和它一起做）；扩展先于启动加载，最多等 5 秒，失败不影响页面；`lovia.version` / `ready` | `templates=` → `assets=` | 小到中 |
| 3 重构 | 拆分 chat.js；建立工具状态模型；两条渲染路径在工具层合一 | 无（纯重构） | 大 |
| 4 `window.lovia` v1 | `on` / `state` / 页面动作 / `api` / `t`；`registerToolView`（内置的 shell、read_file 先用）；`lovia.d.ts`；`examples/31_web_custom_ui` | 只新增 JS 接口 | 中 |
| 5 数据通路 | 先写设计文档并讨论透 §5.7，再实现 `context` / `provideContext` / `run_context` | 新增 | 中 |
| 6 消息标识与消息操作 | 服务端生成的 `ref`；`registerMessageAction`（内置复制先用）；rewind 改用 `ref` | `RewindRequest` 字段变化 | 中 |
| 7 可选 | 工具进度事件（core）；定时任务开关（§5.9）；把 `lovia web init-ui` 做成脚手架。在 [#244](https://github.com/cymoo/lovia/issues/244) 跟踪 | 视方案 | — |

- **顺序**：PR 5 和 PR 6 互不依赖，可以按需求先后调换。定时任务开关很小，可以随时单独做。
- **对 core 的改动**：PR 1–6 都不需要改 core，只用到 core 已有的能力：
  - PR 5 用到 `Runner.run/stream(context=)`；context 已经会传给工具、handoff、`agent_as_tool`（[handoff.py:184](../../lovia/handoff.py#L184)）和内联运行的子 agent（[subagents.py:431](../../lovia/plugins/subagents.py#L431)）；恢复时由调用方重新提供 context，本来就是 core 的约定。
  - PR 6 用到 `Session.segments()` 里的 `run_id`、追加的幂等性，以及 rewind 只截尾的语义。

  只有 PR 7 的工具进度事件需要改 core。另有两个可选的小改动，都只是在 core 里加字段，不必须：
  - **`ChildSpec.context`**：子 agent 的运行函数被替换后，拿不到父运行的 context（`ChildSpec` 里没有这个字段），而内联运行时会传。这是 core 里一个与本方案无关的现有缺口，任何替换了运行函数的用户都会悄悄丢掉 context，值得单独修。PR 5 不依赖它，web 用 `run_context` 重新构造即可。
  - **`RunResult.run_id`**：只为了让"自带 Session 且没有 checkpointer"的部署也能在运行结束时拿到 `ref`。不加，这类部署就要等下次加载历史。
- **合并方式**：每个 PR 单独发版，用 squash merge。
- **文档**：README、README-zh 以及 docs/en、docs/zh 的 web-server / web-ui 同步更新。
- **验证**：
  - `pytest tests/web`
  - `node --test tests/web/js/*.test.mjs`
  - `npx -p typescript@7.0.2 tsc -p lovia/web/static/jsconfig.json`
  - 涉及 UI 的 PR 再用 verify skill 跑一遍端到端。

## 8. 待定的问题

括号里是推荐，等你确认：

1. **定时任务开关的默认值**（§5.9）。推荐嵌入式默认关闭、CLI 开启。
2. **消息操作**：推荐单独成为 PR 6，用服务端生成的 `ref`，等有反馈按钮的实际需求时再做。上一版"沿用 `user_turn`"的推荐已撤回，原因见 P-11。
3. **语言参数**：推荐命名为 `default_lang`，只作为"没选过时的默认值"；需要锁定时，用 `data-part` 隐藏语言选择器。
4. **client context 的持久化**：推荐存在 `chat_sessions.active_context_json`，与 `active_run_id` 同时写入、同时清除。上一版推荐的 `chat_runs` 已撤回，原因见 §5.7。

## 参考

- Chainlit：[Theme](https://docs.chainlit.io/customisation/theme) · [Custom CSS](https://docs.chainlit.io/customisation/custom-css) · [Custom Element](https://docs.chainlit.io/api-reference/elements/custom)
- Gradio：[Custom CSS and JS](https://www.gradio.app/guides/custom-CSS-and-JS) · [Gradio 6 migration](https://www.gradio.app/guides/gradio-6-migration-guide)
- LibreChat：[interface 配置](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/interface)
- assistant-ui：[makeAssistantToolUI](https://www.assistant-ui.com/docs/copilots/make-assistant-tool-ui)
- AI SDK：[Streaming Custom Data](https://ai-sdk.dev/docs/ai-sdk-ui/streaming-data) · [UIMessage](https://ai-sdk.dev/docs/reference/ai-sdk-core/ui-message)
- AG-UI：[Architecture](https://docs.ag-ui.com/concepts/architecture)
- Open WebUI：[Functions](https://docs.openwebui.com/features/extensibility/plugin/functions.md)
