# 语音输入：录一段，转成文字，放进输入框

> 状态：第 1、2 步已实现（#535、v0.9.277；界面 v0.9.278），第 3 步未做。与本文的出入见文末"实现时的出入" · 2026-11-25 · 范围：`lovia/web`（新增 `transcribe.py`、`api/transcribe.py`、`static/js/voice.js`、`static/js/voice-settings.js`；改 `config/schema.py`、`config/runtime.py`、`api/config.py`、`api/__init__.py`、`api/deps.py`、`app.py`、`default.py`、`errors.py`、`static/js/chat.js`、`settings.js`、`main.js`、`i18n.js`、`static/styles.css`）。core 与 workspace 不改，不加依赖。
>
> 行号指向 `main`（`be384429`，v0.9.275）。

## 结论

1. **只做听写**：说话 → 整段录完 → 服务端转写 → 文字插到光标处。不做语音对话，不朗读回复。
2. **没配置就没有**：没有配置转写模型时，麦克风按钮和快捷键都不存在，前端也不加载录音模块。
3. **配置可以沿用已有连接**：选一个已有的聊天模型连接，用它的地址和密钥，再选转写模型。也可以单独填。
4. **两个出口**：✓ 放进输入框检查，↑ 完成并发送。录音时输入框的发送键等于 ✓，发送只有 ↑。没有"自动发送"设置项。
5. **统一手势**：鼠标、触屏、Ctrl+M 都是轻点切换、按住说话。
6. **接口**：浏览器发 `audio/wav` 给 lovia，lovia 按 OpenAI 兼容的 `/audio/transcriptions` 转给上游。录音不落盘，密钥不进浏览器。
7. **可以整块拿掉**：语音不是多数人的刚需。`voice.js` 只通过输入框的几个通用接口接入，别的模块里没有"正在录音"的判断（见"耦合边界"）。

## 交互

录音条只替换底栏里的工具区、用量环、麦克风和发送键，**不改变输入框高度**，文本框始终可以打字。

```
空闲       [+] 项目 模式 模型                  ◔  🎤  ↑
等待授权   ✕  等待麦克风授权…
录音中     ✕  ▁▃▅▂▆▃▁▂▅▃▂  0:07                  ✓  ↑
转写中     ✕  正在转写…
失败       ✕  转写失败：密钥无效                  重试
已取消     已丢弃这段录音                          恢复
运行中     ✕  ▁▃▅▂▆▃▁▂▅▃▂  0:07            [停止]  ✓  ↑
```

运行期间停止键始终可见：停止键是停止 run 的唯一入口（`main.js:209`）。语音从不碰它，仍由 `chat.js` 照常显示和隐藏。

### 开始和结束

| 动作 | 结果 |
| --- | --- |
| 轻点 🎤 / 轻按 Ctrl+M | 开始；再按一次 Ctrl+M 等于 ✓ |
| 按住 🎤 / 按住 Ctrl+M（≥ 400 ms） | 说话，松开等于 ✓ |
| ✓ / 输入框的发送键 | 转写后放进输入框 |
| ↑ | 转写后发送 |
| ✕ / Esc | 取消，输入框不变；转写中取消会中止请求；5 秒内可恢复 |

- 开始后**焦点放在 ✓ 上**，所以 Enter 就是 ✓。
- **录音中，输入框的发送键也等于 ✓，不发送**。发送键指用户设置的那个：`enterToSend()` 关掉时是 ⌘/Ctrl+Enter，触屏上 Enter 永远是换行（`chat.js:5144`）；⌥Enter 同样处理，不 steer。转写中发送键不起作用，和上传中一样。发送只有 ↑。
  - 理由：放进输入框能撤销，发出去收不回。
  - 这样不用管焦点。会把焦点放进输入框的有五处：run 结束（`chat.js:3777`）、`window.lovia` 公开的 `setDraft`（`chat.js:4183`）、选完附件（`chat.js:5127`）、`agent-chip.js:119`、`sessions.js:1480`。逐个拦住，漏一处就是没检查的文字发出去。
  - "Enter 检查，再 Enter 发送"照样成立：第一下是 ✓，焦点回到输入框，第二下发送。
- 审批卡和问题卡只在页面空闲时抢焦点（`chat.js:1491`、`chat.js:1704`），✓ 上有焦点不算空闲，不用改。
- **Esc 取消要让位**：只在焦点位于录音条、输入框或页面空白处，不在输入法组字中、事件没有被别处处理（补全菜单关闭时会 `preventDefault`，`composer-complete.js:195`）、没有打开的对话框时生效。
- **取消可以恢复**：lovia 不用 Esc 停止 run，因为 Esc 是条件反射按的（`main.js:209`）；丢掉一段录音同样收不回。所以 ✕ / Esc 之后录音在内存里再留 5 秒，录音条显示"已丢弃这段录音 · 恢复"。恢复等于 ✓（转写后放进输入框），不续录。失败状态本来就要留着录音，不多一套机制。
- 完成后把焦点放回输入框，但只在焦点位于页面空白处或录音条上时：用户已经在搜索框或对话框里就不抢。

### 按住的细节

- 只认 `Ctrl`（不含 ⌘、⌥、⇧）加 `e.code === 'KeyM'`。用 `code` 不用 `key`，因为输入法激活时 `key` 可能是 `Process`。忽略 `repeat`；有打开的对话框、或页面显示的是别的视图时不响应。要 `preventDefault`：Edge 和 Firefox 把 Ctrl+M 绑在"静音标签页"上（待实测能否拦住）。
- 松开 M 或 Ctrl、窗口失去焦点、触屏 `pointercancel`，都算松开。麦克风按钮在录音时会被替换，所以松开事件监听在 `window` 上。
- 触屏按住：关掉长按菜单和选中（`touch-action: none`、`-webkit-touch-callout: none`、拦 `contextmenu`）。
- 授权弹窗还没答完就松开：按轻点处理，授权后开始录。
- **等待授权也算这一段的一部分**：录音条显示"等待麦克风授权…"。✕、Esc、切换聊天、打开别的视图都让这一段失效；之后才到的授权不开麦，拿到的音轨立即停掉。Chrome 的授权弹窗不挡页面，用户可以先切走再点允许。
- 不足 0.3 秒的录音直接丢弃。

### 录音期间

- 波形用 `AnalyserNode` 的实时音量；`prefers-reduced-motion` 时改为静态电平。`AudioContext` 在手势里同步创建，再等 `getUserMedia`，否则 Safari 上授权之后波形不动。
- 上限 5 分钟，4:30 起倒计时，到时停止并转写（等于 ✓，不发送）。时长按时间戳算：后台标签页的定时器会被节流。
- **停顿不自动停**：思考时的停顿很常见。
- 切到别的浏览器标签页**继续录**，边看边说是正常用法，浏览器自己会显示麦克风标记。
- **麦克风中途没了，就转写已录到的部分**（等于 ✓）：音轨 `ended`（拔掉设备、蓝牙断开、系统收回权限），或页面隐藏时音轨被 `mute`（iOS Safari 进后台会静音采集，上一条在那里不成立）。
- 有正在录或没转写完的录音时，离开页面前提示（`beforeunload`）。

### 切换聊天

**切换聊天、新建聊天、打开别的视图时，立即停止录音并转写**，不留下看不见的麦克风。还在等授权的那一段直接失效。结果归原来的聊天（按 id，见"前端实现要点"的归属）：

- 到达时正显示那个聊天：插到当前光标处。
- 没显示：追加到那个聊天的草稿末尾，并提示"已加入「标题」的草稿"。原本要 ↑ 发送的，也只进草稿。
- 聊天已删除：丢弃。

### 插入

用 `document.execCommand('insertText')` 插到当前光标处，替换选中的文字。它保留撤销，并触发 `input` 事件，自动高度、发送键、草稿保存都跟着走。前后是拉丁字母或数字时补一个空格，中日文不补。

`execCommand` 要求输入框有焦点。不该抢焦点时（见"开始和结束"的最后一条）改用 `setRangeText` 再补发一次 `input` 事件，这一次不能撤销。

### 出错和不可用

| 情况 | 处理 |
| --- | --- |
| 转写失败 | 录音留在页面内存里，可重试；切走再回来还在 |
| ↑ 之后转写失败 | 重试成功后只放进输入框，不发送：隔了一次失败，先让人看一眼 |
| 识别结果为空 | 提示"没有识别到文字"，输入框不变；原本要 ↑ 发送的也不发，即使输入框里已有文字 |
| 反代拒绝请求体（413，且响应不是 lovia 的 JSON） | 提示"代理限制了请求体大小"和 `client_max_body_size`；不给重试，重试不会成功 |
| 全程近乎无声 | 不上传，提示"没有收到声音，检查麦克风" |
| 浏览器拒绝麦克风 / 没有设备 | 说明原因和去哪里允许 |
| 页面不是 HTTPS 或 localhost（如局域网 HTTP） | 按钮保留，点了说明原因 |

最后一行和"没配置"不同：配置在、只是这个页面不能录，藏起来会让人以为配置丢了。无声检测只拦静音或未接通的麦克风，阈值（暂定峰值 −60 dBFS）按真实设备调，**宁可多传，也不误拦轻声**。

## 配置

`config.json` 加一段 `voice`，不放进 `models`：转写模型不能聊天，放进去会出现在聊天的模型选择器里。

```python
class VoiceConfig(BaseModel):
    connection: str | None = None  # 沿用这个模型配置的地址和密钥
    base_url: str | None = None    # 或单独填；与 connection 二选一
    api_key: str | None = None
    model: str | None = None       # 如 "gpt-transcribe"
    language: str | None = None    # ISO 639-1；None 为自动
```

- **启用条件**：有 `model`，并且连接解析得出来：`connection` 指向一个存在的、OpenAI 接口的模型配置，或者有 `base_url`。否则就是没配置。
- `base_url` 是 API 根，服务端拼 `/audio/transcriptions`，和现有 `{base_url}/models` 一致（`config/probe.py:51`）。
- **`connection` 解析不出来，一律视为没配置，不报错**：指向的模型被删了、改成了 Anthropic 接口、或者手改时写错了 id，都一样，设置页说明原因（如"连接 X 已不存在"）。不像 `roles` 那样拒绝加载（`schema.py` 的 `_check_references`），也不在删除模型时回头清理（`api/config.py:503`）：改模型、删模型不该被一个不相干的设置拦住，模型配置的代码也不必知道语音。
- 引用而不是复制：以后换密钥，语音跟着变。
- 密钥在 API 里打码、沿用"不传保留、空串清除"的约定；保存走 `ConfigRuntime.apply(settings_only=True)`，还没配聊天模型时也能存。

### 设置页

```
设置 → 语音输入
  连接   [ 关闭 | 沿用已有连接 ▾ | 自定义 ]
  模型   [ gpt-transcribe ▾ ]
  语言   [ 自动 ▾ ]
  录音会发送到 api.openai.com 转写，lovia 不保存录音。
  [ 试录一句 ]
```

- 页面是独立的 `voice-settings.js`，`settings.js` 只在 `groups` 里加一个条目（`settings.js:307`），和 `memoryPane`、`mcpPane` 一样。
- "沿用已有连接"只列 OpenAI 接口的模型配置。连接失效时在这里说明原因，不让它看起来像配置丢了。
- 模型建议：用 `POST /api/config/test` 拿到连接的模型列表，按 `transcri|whisper|asr|sense-?voice|voxtral` 筛。列不出来（端点不列、自定义连接没有刚填的密钥）就手填。
- **试录一句**在保存后出现，走的就是 `/api/transcribe`，一次验证麦克风、密钥、模型和语言。只查 `/models` 证明不了能转写。它只有几秒，测不出反代的请求体上限；真撞上时由 413 的专门提示说明（见"出错和不可用"）。
- **不自动启用**：已有 OpenAI 密钥也要用户自己选，因为会上传录音并产生费用。

## 接口

`POST /api/transcribe`，请求体是 `audio/wav`，返回 `{"text": "…"}`。

| 状态 | 码 | 情况 |
| --- | --- | --- |
| 404 | `feature_unavailable` | 没配置 |
| 415 | `unsupported_file_type` | 不是 `audio/wav` |
| 413 | `file_too_large` | 超过 10 MiB |
| 422 | `invalid_request` | 空请求体 |
| 502 | `transcription_failed`（新增） | 上游出错、超时、连不上 |

- **直接收 WAV，不用 `UploadFile`**：Starlette 的 multipart 超过 1 MiB（约 33 秒录音）就写临时文件，而且 FastAPI 在鉴权依赖之前就解析整个表单。用 `Request.stream()` 边收边限量：鉴权没通过时一个字节都不读，也不落盘。两点都实测过。Content-Type 和 Content-Length 在读请求体之前检查。
- **上游的 401 不透传**：前端把任何 401 当作登录失效处理（`api.js:44`）。一律 502，原因和"去设置 → 语音输入检查"写在 message 和 hint 里，截短。前端只有一个动作（重试），所以不再细分错误码。
- 不解析 WAV 头、不算时长：10 MiB 已经限住，格式错误由上游拒绝。
- `audio/wav` 不是简单请求类型，跨站请求会先过 CORS 预检，别的网页不能借用户的浏览器花钱。token 的 cookie 本身是 `SameSite=Strict`（`main.js:69`）。
- **不单独加 Host 检查**。无 token 的本机部署下，DNS 重绑定能绕过预检；但那样的页面同样能调 `/api/chat` 让 agent 跑工具、读聊天和文件，现在只有配置写入、`/test`（`api/config.py:292`）和桌面动作查 Host。只挡转写没有意义，要挡就挡整个 API，那是另一件事。
- 上游请求用 `httpx.AsyncClient`，`verify` 和代理设置与 provider 相同（`resolve_verify`、`resolve_trust_env`），超时 60 秒，不自动重试。

### 语言字段

- `gpt-transcribe` 用 `languages[]` 取代了 `language`，两个不能同时发；其他模型用 `language`。按模型名是否含 `gpt-transcribe` 区分。
- 语言为自动时两个都不发。
- 这个选项进第一步，不后置：ServiceNow 的 CoSE-E 测试里，Whisper large-v3-turbo 在不指定语言时把中英混说**翻译**了，字错率 251.6%（合成语音）。

### 能力标记和扩展点

- `create_app(transcribe=fn)`，`fn: TranscribeFn = Callable[[TranscriptionRequest], Awaitable[str]]`。`TranscriptionRequest` 是 dataclass，现在只有 `audio: bytes`（16 kHz 单声道 16-bit WAV）。第 3 步的提示词加字段，不改签名，和 `FollowupFn` 收 `FollowupRequest` 一样（`followups.py:97`）。
- **内置实现是公开的**：`lovia.web.transcribe.openai_transcriber(base_url=, api_key=, model=, language=None)` 返回一个 `TranscribeFn`。用 `create_app` 的部署方没有 `VoiceConfig`，这是他们的入口：`create_app(transcribe=openai_transcriber(...))`。`ConfigRuntime` 按当前配置调用同一个函数，multipart 和语言字段的分支只写一遍。
- 别的引擎（如走 chat/completions 的阿里云 `qwen3-asr-flash`）自己写一个 `TranscribeFn`。
- `default_app` **不加**这个参数：它的配置来自设置页，加了就要规定谁优先。它把 `ConfigRuntime` 的方法传给 `create_app`，和 `followups` 一样（`default.py:134`）。
- `/api/info` 加 `features.transcription`，按请求时的配置算。**不能照 `followups` 写成 `bool(deps.transcribe)`**（`api/__init__.py:87`）：`default_app` 传的是绑定方法，永远为真。followups 关掉时返回空列表就行，转写要 404 和藏按钮。所以有 `config_runtime` 时问它当前配置是否启用，否则看 `transcribe` 传没传；接口的 404 用同一个判断。
- 页面现在只在启动时读一次 `/api/info`，所以保存设置后要重取，麦克风才会立即出现或消失。由语音自己的启动桩订阅 `config-changed` 来做（见"耦合边界"），不改 `model-config.js`。

## 耦合边界

语音不是多数人的刚需，所以它要能整块拿掉，也不能让别的功能为它多背分支。规则：

**`voice.js` 可以引用下面几个通用接口和 `store` 的事件。除了一行懒加载，没有模块引用 `voice.js`；别的模块里没有"正在录音"的判断。**

`chat.js` 加三个导出，都不含"语音"：

| 导出 | 作用 | 语音怎么用 |
| --- | --- | --- |
| `gateSend(fn)` | 提交处理的开头（`chat.js:5197`）先问闸门，`fn` 返回真表示这次提交它接了；返回注销函数 | 录音中：做 ✓。转写中：拦住 |
| `insertText(text)` | 在屏幕上这个聊天的输入框光标处插入（见"插入"） | 结果到达时正显示那个聊天 |
| `appendDraft(chatId, text)` | 往不在屏幕上的聊天的草稿末尾追加 | 结果到达时没显示 |

另外导出已有的 `attachOwner()`（`chat.js:4307`），见下面的"归属"。闸门接了提交要复位 `_steerOnce`，和上传中的提前返回一样（`chat.js:5216`）。

其余都在 `voice.js` 里，用的是已有的东西：

| 需要 | 做法 |
| --- | --- |
| 麦克风按钮 | 自己创建，挂到 `[data-part="composer-end"]`。不改模板 |
| 录音条换掉工具区、用量环和发送键 | 在 `#composer` 上设 `data-voice="…"`，`styles.css` 里一段规则隐藏它们。录音条、✓、↑ 是 `voice.js` 自己的元素；↑ 转写成功后调 `requestSubmit()` |
| 切换、新建、删除聊天 | 订阅已有的 `session-switched`、`new-chat`、`session-deleted`（`sessions.js:1391`、`:1479`、`:1369`） |
| 打开别的视图 | `lovia.on('view')` |
| Ctrl+M、Esc、`beforeunload` | 自己的监听 |
| 启用和停用 | `main.js` 里的启动桩：`features.transcription` 为真时 `import('./voice.js')`；订阅 `config-changed`（`sessions.js:302`）重取 `/api/info` |

躲不掉的三处都是数据，不是逻辑：`settings.js` 的一个条目、`i18n.js` 的文案、`/api/info` 的一行。快捷键写在麦克风按钮的提示里，不进快捷键帮助表（`main.js:167`）。

- **检验**：拿掉这个功能，只删 `voice.js`、`voice-settings.js`、`styles.css` 的一段、`main.js` 的启动桩、`settings.js` 的一个条目，以及后端的路由和配置段。`chat.js` 的三个导出留着也无害。
- **这些接口留在内部，不放上 `window.lovia`**：放上去就进了 1.0 契约，等有第二个使用者再说。
- 后端本来就是独立的：一个路由、一个配置段、`create_app` 的一个参数，不加依赖。

## 前端实现要点

- **录音**：`MediaRecorder` 录浏览器的默认格式，结束后用 `OfflineAudioContext(1, 1, 16000).decodeAudioData` 直接以 16 kHz 解码，混成单声道，写成 16-bit WAV。统一成 WAV，上游就不用管各浏览器的容器格式。5 分钟约 9.6 MB，低于 OpenAI 的 25 MB。
- **加载**：`features.transcription` 为真时才 `import('./voice.js')`，按钮由它创建。每段录完就停掉音轨，让浏览器的麦克风标记消失。
- **归属**：一段录音属于开始时屏幕上那个聊天的 id，取自 `attachOwner()`。已有聊天是它自己的 id；新聊天是预先生成、首次发送会带上的那个 id（`chat.js:3848`），附件已经这样做。
  - 同时只有一段在录；转写中、失败、已取消的录音按这个 id 各存一份。
  - 结果到达时 id 等于 `attachOwner()` 就是"正显示"，用 `insertText`；否则用 `appendDraft`。新聊天的草稿键仍是 `'new'`（`chat.js:4194`），由 `appendDraft` 换算。
  - **不在 `session` 事件里改键**，`applyEvent` 不动。原先的做法是把 `'new'` 的录音在首帧到达时改成真 id（`chat.js:3075`），但切走聊天会中止 SSE（`chat.js:4068`），首帧不到，改键就不发生。
  - 剩下的窗口见"接受的代价"第 8 条。
- **发送**：录音和转写期间，这个聊天的提交由闸门接走。↑ 转写成功后走正常的提交：运行中照常排队，需要先选目录时照常弹出，退出就留在草稿里。
- **取消**：每段录音一个对象和一个 `AbortController`，从按下那一刻算起，包括等待授权。写回、开麦之前都确认它还是当前那段，迟到的结果不会写回，迟到的授权不会开麦。
- 不做"文本版本"追踪：用户在转写时继续打字，文字插到当前光标处，不改成追加到末尾。

## 不做和否决

| 方案 | 为什么不做 |
| --- | --- |
| 浏览器 Web Speech | 各浏览器支持不一，本机识别在 Safari 上没有；零配置的需求用系统自带的听写就能满足 |
| 边说边出字 | 流式要 WebSocket 和各家私有协议；按停顿切段会丢上下文，中英混说更容易错。Cursor 3.1 为了准确率改成了整段转写 |
| 内置 whisper.cpp 或把它当推荐 | 程序和模型太大；调研的项目多数不用；Homebrew 的包不带 `whisper-server`；Whisper 不指定语言时中英混说不可靠（见"语言字段"）。它提供兼容接口，想用的人填地址就行 |
| 音频直接发给聊天模型 | 要给 core 和每个 provider 加音频，而且不是每个模型都支持 |
| 录音存成附件 | 转写后原音没有用处，还要管清理 |
| 按空格说话 | 和打字、中文输入法切换键冲突 |
| 空输入框时麦克风占发送键的位置 | 按钮会跳，打了一半字再补语音不方便 |
| 自动发送设置、免手对话、全局热键、LLM 润色、朗读 | ↑ 已覆盖主要需求，其余看使用情况 |
| `transcription_auth` 等细分错误码 | 前端不按码分支 |
| 适配 Firefox | 占有率低。不屏蔽，能用就用，不测试 |
| 录音时拦住别处把焦点移进输入框 | 要拦五处，其中一处是公开接口；漏一处就是没检查的文字发出去。改成发送键等于 ✓（见"开始和结束"） |
| 在 `voice.js` 里用捕获阶段截 Enter 或 `submit` | 会抢在补全菜单之前；⌥Enter 已经置位的 `_steerOnce`（`chat.js:5149`）没人复位，下一条会被当成 steer。用 `gateSend` |
| 在 `session` 事件里给录音改键 | 切走后首帧不到；附件已有的预生成 id 更简单，也不用动 `applyEvent` |
| 录音时不播完成提示音 | 要让 `settings.js` 知道录音状态。`getUserMedia` 默认开回声消除，本页播放的声音多半录不进去；第 2 步实测，真录进去了再加 |
| 校验 `connection` 的引用、删除模型时清理 | 见"配置" |
| 把输入框的接口放上 `window.lovia` | 进 1.0 契约，现在只有一个使用者 |
| 只给 `/api/transcribe` 加 Host 检查 | 见"接口" |

## 接受的代价

1. 刷新页面会丢掉还没转写的录音，离开前有提示。
2. 取消转写时上游可能已经计费。
3. 用户只配了 Anthropic、DeepSeek 这类连接时，要另配一个转写服务，或者用系统听写。
4. 浏览器禁用存储时，草稿本来就不保存，切走的录音转写后也存不下来。
5. 语言是服务器级设置，不分用户。
6. 反代要放开请求体大小（nginx 默认 1 MB，约 33 秒录音）。附件上传已有同样的问题，文档写一行 `client_max_body_size`。"试录一句"测不出来，撞上时页面有专门的提示。
7. 用 iframe 嵌入的部署方要自己加 `allow="microphone"`。
8. 新聊天的首次发送发出后、首帧到达前（本机上是毫秒级）开始的录音，文字可能进下一个新聊天的草稿，而不是刚建的那个聊天：预生成的 id 要到首帧才和聊天对上（`chat.js:3074`）。文字没有发出去，看得见，能剪走。这个窗口来自 `_draftId` 现有的生命周期，要修就修在那里，不在语音里另做一套。
9. 模型名是部署别名（Azure、LiteLLM）时，按名字判断 `gpt-transcribe` 会发错语言字段，上游返回 400。语言设成自动可以绕开。
10. 取消后的恢复只有 5 秒，过后录音就释放了。

## 和其他 harness 的对照

（2026-11 调研，以官方文档和源码为准，另注明的除外。）

| 产品 | 触发 | 转写 | 转写后 |
| --- | --- | --- | --- |
| Claude Code | 按住空格，或点按模式 | 流式，云端；带项目名和分支名作提示 | 按住模式默认不发送；点按模式满 3 词自动发送；Esc 取消 |
| Cursor | 麦克风按钮；按住 Ctrl+M | 3.1 起整段录完再转写 | 进输入框；有波形、计时、取消和确认 |
| ChatGPT 桌面端（含 Codex） | Dictate 按钮 | 未说明 | 进输入框，检查后发送 |
| VS Code | 麦克风按钮或快捷键 | 桌面端默认本机模型，网页版走云端 | 不自动发送；默认用 LLM 整理 |
| Gemini CLI（实验） | 空格，按住或切换 | Gemini Live，或本机 whisper.cpp | 进输入框（第三方资料） |
| Kiro CLI | `/voice` 或按住空格 | 本机 whisper.cpp | 默认自动发送 |
| Goose | 未查 | OpenAI、Groq、ElevenLabs，或本机 Whisper（Candle） | 未查 |
| Open WebUI | 麦克风按钮，✓ / ✕ | 本机 faster-whisper、OpenAI 兼容接口、浏览器识别等 | 可选自动发送；服务端保留录音文件 |
| LibreChat | 按钮或 Shift+Alt+L | 浏览器识别或 OpenAI 兼容接口 | 可设延迟自动发送 |

- 听写和语音对话是两个功能：Cursor、VS Code、ChatGPT 都分开做。
- 多数让用户检查后再发送。我们用 ✓ 和 ↑ 把这个选择交给每一次，不做成设置。
- Open WebUI 的文档写明服务端会保留录音文件；我们不保存。

能直接接的服务（按各自文档，未实测）：OpenAI `gpt-transcribe`、Groq `whisper-large-v3-turbo`、SiliconFlow SenseVoice、vLLM、speaches。OpenRouter 的 API 文档写支持 multipart，它自己的 skill 页说不支持，实测后再写进文档。`whisper-1` 和 `gpt-4o-transcribe` 系列 2027-02-26 下线，示例只用 `gpt-transcribe`。

## 分步

1. **接口和配置**：`transcribe.py`（含公开的 `openai_transcriber`）、`POST /api/transcribe`、`VoiceConfig`、`PUT /api/config/voice`、`create_app(transcribe=)`、`features.transcription`。不含设置页，免得发布一个没有麦克风的设置页。用真实录音试 `gpt-transcribe` 和 Groq 的短中文、中英混说、项目名，据此定语言默认值和建议。
   - 这一步就是公开行为变更，文档跟着走：`docs/{en,zh}` 的 `http-api.md`、`web-server.md`，以及 `deployment.md` 的 `client_max_body_size`。
   - `stability.md` 里把 `/api/transcribe`、`create_app(transcribe=)`、`voice` 配置段标为实验性，第 2 步真实用过再定进 1.0 契约。
2. **界面**：`chat.js` 的三个导出、设置页和试录、录音条、快捷键、草稿归属、中英文案；同步两份 README 和 `docs/{en,zh}` 的 `web-ui.md`。
3. **按需**：自定义词表，带上项目名和分支名作提示（`prompt`、`keywords`）。

## 测试要点

第 1 步：

- 没带 token：401，请求体一个字节都没读；超过 10 MiB 在读完之前就 413；全程不产生临时文件。
- 上游 401、403、429、500、超时、断开、非 JSON、没有 `text`：都是 502 `transcription_failed`，不出现 401；密钥不出现在响应里。
- `gpt-transcribe` 发 `languages[]`，其他模型发 `language`，自动时都不发。
- 沿用连接：改那个模型的密钥后立即生效；删掉那个模型、改成 Anthropic 接口、手改成不存在的 id，语音都变成没配置，配置照常加载和保存。
- 没配置时 `features.transcription` 为假、接口 404；`default_app` 下不因为传了绑定方法就为真。
- `create_app(transcribe=openai_transcriber(...))`：没有 `ConfigRuntime` 也能转写，`features.transcription` 为真。
- Content-Type 不是 `audio/wav`：读请求体之前就 415。

第 2 步（合并条件：Chromium 用假麦克风音频自动测试，Safari 用真实麦克风手测，Edge 手测 Ctrl+M）：

- 没配置：没有按钮，Ctrl+M 不响应，没有请求 `voice.js`；保存设置后不刷新就出现，清空后不刷新就消失。
- 轻点、按住两种手势在按钮和 Ctrl+M 上都成立；中文输入法激活时 Ctrl+M 有效。Edge 上 Ctrl+M 不会静音标签页。
- **发送键等于 ✓**：录音中焦点在输入框，按发送键（两种 Enter 设置、⌥Enter）都只放进输入框，不发送，下一条消息不会被当成 steer；补全菜单打开时 Enter 仍是选中条目。转写中发送键不起作用。
- 录音中 run 结束、扩展调用 `setDraft`：都不会导致发送。运行中录音：停止键可见可用，↑ 进入排队。
- **等待授权**：授权前切换聊天、按 ✕ 或 Esc，之后再允许：不开麦，浏览器的麦克风标记不出现。
- **Esc**：补全菜单打开时、输入法组字中、有对话框时，Esc 不取消录音。取消后 5 秒内恢复能拿回文字，过后不能。
- 录音中切换聊天：麦克风关闭，文字进原聊天草稿；切回来时在；原聊天被删时丢弃。
- 新聊天里录音，再首次发送：文字进这个聊天，不进下一个新聊天。新聊天草稿里有失败的录音时在别的聊天发送：那段录音仍属于新聊天草稿。
- 转写中取消、重试、取消后响应才到：不重复插入，不写回。
- 失败后重试成功；切走再回来仍可重试。↑ 之后失败再重试成功：只放进输入框。↑ 之后识别为空：不发送。
- 录音中拔掉麦克风（Chromium 里停掉音轨）：转写已录到的部分。iOS Safari 进后台：同样。
- 反代返回 413（HTML）：显示代理的提示，没有重试。
- 完成时焦点在搜索框：文字进了输入框，焦点没被抢走。
- **拿掉检验**：不加载 `voice.js` 时，`chat.js` 的三个导出没有调用方，其余行为和 `main` 一致。
- 5 分钟录音的内存和转换时间；4:30 倒计时；到时只放进输入框，不发送。
- 局域网 HTTP：按钮在，点了有说明。

## 实现时的出入

- **`default_app` 不向 `create_app` 传转写函数**。`RouterDeps.transcriber()` 在有 `config_runtime` 时直接问它，少一个"永远为真"的值。
- **导出的是 `composerChat({ mint })`，不是 `attachOwner()`**。判断"正显示"只需要看一眼 id；直接调 `attachOwner()` 会给每个新聊天预生成 id，改变所有人首次发送的方式。只有开始录音时才 `mint`。
- **多了一个 `voice-recorder.js`**：采集和转 WAV 不依赖页面，输入框和设置页的"试录一句"共用。`api.js` 加了 `transcribe`、`setVoice` 两个方法。
- **沿用连接时，文件里没有密钥就用 `OPENAI_API_KEY`**，和聊天的 provider 一致（评审发现）。
- **密钥里有请求头放不下的字符（换行、弯引号）时，发请求之前就拒绝**：HTTP 库的报错会把密钥原样带出来（评审发现）。
- **麦克风按钮没有 `data-part`**：那是公开契约，实验期不加。
- **未验证**：真实麦克风、真实转写服务、Safari、Edge 的 Ctrl+M、5 分钟录音、回声消除能否挡住提示音。自动化验证用的是合成音源和假转写服务（这个环境里 Chromium 的假麦克风不响应）。

已做的小实验：用合成音源跑"录音 → WAV"，Chromium 145（webm/opus）和 WebKit 26（mp4/aac）的 2 秒录音转换各约 15 ms，`execCommand` 插入后可撤销。真实麦克风、转写接口和 5 分钟录音都没测。
