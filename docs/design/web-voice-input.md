# 语音输入：录一段，转成文字，放进输入框

> 状态：设计定稿，未实现 · 2026-11-25 · 范围：`lovia/web`（新增 `transcribe.py`、`api/transcribe.py`、`static/js/voice.js`；改 `config/schema.py`、`config/runtime.py`、`api/config.py`、`api/__init__.py`、`app.py`、`errors.py`、`static/js/chat.js`、`settings.js`、`main.js`）。core 与 workspace 不改，不加依赖。
>
> 行号指向 `main`（`be384429`，v0.9.275）。

## 结论

1. **只做听写**：说话 → 整段录完 → 服务端转写 → 文字插到光标处。不做语音对话，不朗读回复。
2. **没配置就没有**：没有配置转写模型时，麦克风按钮和快捷键都不存在，前端也不加载录音模块。
3. **配置可以沿用已有连接**：选一个已有的聊天模型连接，用它的地址和密钥，再选转写模型。也可以单独填。
4. **两个出口**：✓ 放进输入框检查，↑ 完成并发送。没有"自动发送"设置项。
5. **统一手势**：鼠标、触屏、Ctrl+M 都是轻点切换、按住说话。
6. **接口**：浏览器发 `audio/wav` 给 lovia，lovia 按 OpenAI 兼容的 `/audio/transcriptions` 转给上游。录音不落盘，密钥不进浏览器。

## 交互

录音条只替换底栏里的工具区和麦克风、发送键，**不改变输入框高度**，文本框始终可以打字。

```
空闲     [+] 项目 模式 模型                  ◔  🎤  ↑
录音中   ✕  ▁▃▅▂▆▃▁▂▅▃▂  0:07                  ✓  ↑
转写中   ✕  正在转写…
失败     ✕  转写失败：密钥无效                  重试
运行中   ✕  ▁▃▅▂▆▃▁▂▅▃▂  0:07            [停止]  ✓  ↑
```

运行期间停止键始终可见：停止键是停止 run 的唯一入口（`main.js:209`），录音条不能盖住它。

### 开始和结束

| 动作 | 结果 |
| --- | --- |
| 轻点 🎤 / 轻按 Ctrl+M | 开始；再按一次 Ctrl+M 等于 ✓ |
| 按住 🎤 / 按住 Ctrl+M（≥ 400 ms） | 说话，松开等于 ✓ |
| ✓ | 转写后放进输入框，焦点回到输入框 |
| ↑ | 转写后发送 |
| ✕ / Esc | 取消，输入框不变；转写中取消会中止请求 |

- 开始后**焦点放在 ✓ 上**，所以 Enter 就是 ✓。"Enter 检查，再 Enter 发送"不用改输入框的 Enter 规则。
- 焦点在输入框里时按 Enter，照旧是发送：录音中等于 ↑（⌥Enter 同样处理，不 steer）。
- 因此**录音和转写期间，别处不能把焦点移进输入框**。run 结束时 `exitStreamingUI` 会无条件 `promptEl.focus()`（`chat.js:3777`）：不拦住的话，想按 ✓ 的 Enter 会变成直接发送。审批卡和问题卡已经只在页面空闲时抢焦点，✓ 上有焦点不算空闲，不用改。
- Esc 只在录音条处于录音或转写状态时生效。lovia 没有用 Esc 停止 run，不冲突。

### 按住的细节

- 只认 `Ctrl`（不含 ⌘、⌥、⇧）加 `e.code === 'KeyM'`。用 `code` 不用 `key`，因为输入法激活时 `key` 可能是 `Process`。忽略 `repeat`；有打开的对话框、或页面显示的是别的视图时不响应。
- 松开 M 或 Ctrl、窗口失去焦点、触屏 `pointercancel`，都算松开。麦克风按钮在录音时会被替换，所以松开事件监听在 `window` 上。
- 授权弹窗还没答完就松开：按轻点处理，授权后开始录。
- 不足 0.3 秒的录音直接丢弃。

### 录音期间

- 波形用 `AnalyserNode` 的实时音量；`prefers-reduced-motion` 时改为静态电平。
- 上限 5 分钟，4:30 起倒计时，到时停止并转写（等于 ✓，不发送）。
- **停顿不自动停**：思考时的停顿很常见。
- 录音时不播完成提示音（`settings.js:99`），避免录进去。
- 切到别的浏览器标签页**继续录**，边看边说是正常用法，浏览器自己会显示麦克风标记。
- 有正在录或没转写完的录音时，离开页面前提示（`beforeunload`）。

### 切换聊天

**切换聊天、新建聊天、打开别的视图时，立即停止录音并转写**，不留下看不见的麦克风。结果归原来的聊天：

- 到达时正显示那个聊天：插到当前光标处。
- 没显示：追加到那个聊天的草稿末尾，并提示"已加入「标题」的草稿"。原本要 ↑ 发送的，也只进草稿。
- 聊天已删除：丢弃。

### 插入

用 `document.execCommand('insertText')` 插到当前光标处，替换选中的文字。它保留撤销，并触发 `input` 事件，自动高度、发送键、草稿保存都跟着走。前后是拉丁字母或数字时补一个空格，中日文不补。

### 出错和不可用

| 情况 | 处理 |
| --- | --- |
| 转写失败 | 录音留在页面内存里，可重试；切走再回来还在 |
| 识别结果为空 | 提示"没有识别到文字"，输入框不变 |
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

- **启用条件**：有 `model`，并且有 `connection` 或 `base_url`。否则就是没配置。
- `base_url` 是 API 根，服务端拼 `/audio/transcriptions`，和现有 `{base_url}/models` 一致（`config/probe.py:51`）。
- `connection` 指向不存在的 id 时拒绝加载，和 `roles` 一样（`schema.py` 的 `_check_references`）。删除那个模型时一并清空 `connection`（`api/config.py:503`）。
- 那个模型后来改成 Anthropic 接口：视为没配置，不报错，否则改模型会被一个不相干的设置拦住。
- 引用而不是复制：以后换密钥，语音跟着变。
- 密钥在 API 里打码、沿用"不传保留、空串清除"的约定；保存走 `ConfigRuntime.apply(settings_only=True)`，还没配聊天模型时也能存。

### 设置页

```
设置 → 语音输入
  连接   [ 关闭 | 沿用已有连接 ▾ | 自定义 ]
  模型   [ gpt-transcribe ▾ ]
  语言   [ 自动 ▾ ]
  录音会发送到 api.openai.com 转写，lovia 不保存录音。
  [ 试录一句 ]   → 把 auth 中间件改成用新的 token 校验函数
```

- "沿用已有连接"只列 OpenAI 接口的模型配置。
- 模型建议：用 `POST /api/config/test` 拿到连接的模型列表，按 `transcri|whisper|asr|sense-?voice|voxtral` 筛。列不出来（端点不列、自定义连接没有刚填的密钥）就手填。
- **试录一句**在保存后出现，走的就是 `/api/transcribe`，一次验证麦克风、密钥、模型和语言。只查 `/models` 证明不了能转写。
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

- **直接收 WAV，不用 `UploadFile`**：Starlette 的 multipart 超过 1 MiB（约 33 秒录音）就写临时文件，而且 FastAPI 在鉴权依赖之前就解析整个表单。用 `Request.stream()` 边收边限量：鉴权没通过时一个字节都不读，也不落盘。两点都实测过。
- **上游的 401 不透传**：前端把任何 401 当作登录失效处理（`api.js:44`）。一律 502，原因和"去设置 → 语音输入检查"写在 message 和 hint 里，截短。前端只有一个动作（重试），所以不再细分错误码。
- 不解析 WAV 头、不算时长：10 MiB 已经限住，格式错误由上游拒绝。
- `audio/wav` 不是简单请求类型，跨站请求会先过 CORS 预检，别的网页不能借用户的浏览器花钱。
- 上游请求用 `httpx.AsyncClient`，`verify` 和代理设置与 provider 相同（`resolve_verify`、`resolve_trust_env`），超时 60 秒，不自动重试。

### 语言字段

- `gpt-transcribe` 用 `languages[]` 取代了 `language`，两个不能同时发；其他模型用 `language`。按模型名是否含 `gpt-transcribe` 区分。
- 语言为自动时两个都不发。
- 这个选项进第一步，不后置：ServiceNow 的 CoSE-E 测试里，Whisper large-v3-turbo 在不指定语言时把中英混说**翻译**了，字错率 251.6%（合成语音）。

### 能力标记和扩展点

- `/api/info` 加 `features.transcription`，按请求时的配置算。页面现在只在启动时读一次 `/api/info`，`config_changed` 只刷新 `/api/config`（`model-config.js:1283`），所以要在 `config-changed` 时重取，保存后麦克风立即出现或消失。
- `create_app(transcribe=fn)`，`fn: Callable[[bytes], Awaitable[str]]`，收 WAV 返回文字，用来接别的引擎（如走 chat/completions 的阿里云 `qwen3-asr-flash`）。
- `default_app` **不加**这个参数：它的配置来自设置页，加了就要规定谁优先。`default_app` 自己把 `ConfigRuntime` 的实现传给 `create_app`，和 `followups` 一样。

## 前端实现要点

- **录音**：`MediaRecorder` 录浏览器的默认格式，结束后用 `OfflineAudioContext(1, 1, 16000).decodeAudioData` 直接以 16 kHz 解码，混成单声道，写成 16-bit WAV。统一成 WAV，上游就不用管各浏览器的容器格式。5 分钟约 9.6 MB，低于 OpenAI 的 25 MB。
- **加载**：`features.transcription` 为真时才 `import('./voice.js')`。按钮在模板里默认隐藏。每段录完就停掉音轨，让浏览器的麦克风标记消失。
- **归属**：同时只有一段在录；转写中和失败的录音按聊天键各存一份。草稿的键是 `sessionId || 'new'`（`chat.js:4194`），所以新聊天首次发送、收到 `session` 事件时要把 `'new'` 的那份改成真正的 id，和附件托盘在同一处（`chat.js:3075`）。不改的话，迟到的文字会落进下一个新聊天。已有聊天发送时也会收到 `session` 事件，所以只在事件到来前 `sessionId` 为空时改。
- **发送**：某个聊天有录音在转写时，它的发送键不可用，和上传中相同（`uploading()`）。↑ 转写成功后走正常的提交：运行中照常排队，需要先选目录时照常弹出，退出就留在草稿里。
- **取消**：每段录音一个对象和一个 `AbortController`。写回前确认它还是当前那段，迟到的结果不会写回。
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

## 接受的代价

1. 刷新页面会丢掉还没转写的录音，离开前有提示。
2. 取消转写时上游可能已经计费。
3. 用户只配了 Anthropic、DeepSeek 这类连接时，要另配一个转写服务，或者用系统听写。
4. 浏览器禁用存储时，草稿本来就不保存，切走的录音转写后也存不下来。
5. 语言是服务器级设置，不分用户。
6. 反代要放开请求体大小（nginx 默认 1 MB，约 33 秒录音）。附件上传已有同样的问题，文档写一行 `client_max_body_size`。
7. 用 iframe 嵌入的部署方要自己加 `allow="microphone"`。

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

1. **接口和配置**：`transcribe.py`、`POST /api/transcribe`、`VoiceConfig`、`PUT /api/config/voice`、`create_app(transcribe=)`、`features.transcription`。不含设置页，免得发布一个没有麦克风的设置页。用真实录音试 `gpt-transcribe` 和 Groq 的短中文、中英混说、项目名，据此定语言默认值和建议。
2. **界面**：设置页和试录、录音条、快捷键、草稿归属、中英文案；同步两份 README 和 `docs/{en,zh}`（`web-ui.md`、`web-server.md`、`http-api.md`、`deployment.md`）。
3. **按需**：自定义词表，带上项目名和分支名作提示（`prompt`、`keywords`）。

## 测试要点

第 1 步：

- 没带 token：401，请求体一个字节都没读；超过 10 MiB 在读完之前就 413；全程不产生临时文件。
- 上游 401、403、429、500、超时、断开、非 JSON、没有 `text`：都是 502 `transcription_failed`，不出现 401；密钥不出现在响应里。
- `gpt-transcribe` 发 `languages[]`，其他模型发 `language`，自动时都不发。
- 沿用连接：改那个模型的密钥后立即生效；删掉那个模型后语音变成没配置；改成 Anthropic 接口后变成没配置且能保存。
- 没配置时 `features.transcription` 为假、接口 404。

第 2 步（合并条件：Chromium 用假麦克风音频自动测试，Safari 用真实麦克风手测）：

- 没配置：没有按钮，Ctrl+M 不响应，没有请求 `voice.js`；保存设置后不刷新就出现，清空后不刷新就消失。
- 轻点、按住两种手势在按钮和 Ctrl+M 上都成立；中文输入法激活时 Ctrl+M 有效。
- 录音中 run 结束：焦点仍在 ✓，Enter 不会发送。运行中录音：停止键可见可用，↑ 进入排队。
- 录音中切换聊天：麦克风关闭，文字进原聊天草稿；切回来时在；原聊天被删时丢弃。
- 新聊天里录音，同时首次发送已发出：文字进这个聊天，不进下一个新聊天。新聊天草稿里有失败的录音时在别的聊天发送：那段录音仍属于新聊天草稿。
- 转写中取消、重试、取消后响应才到：不重复插入，不写回。
- 失败后重试成功；切走再回来仍可重试。
- 5 分钟录音的内存和转换时间；4:30 倒计时；到时只放进输入框，不发送。
- 局域网 HTTP：按钮在，点了有说明。

已做的小实验：用合成音源跑"录音 → WAV"，Chromium 145（webm/opus）和 WebKit 26（mp4/aac）的 2 秒录音转换各约 15 ms，`execCommand` 插入后可撤销。真实麦克风、转写接口和 5 分钟录音都没测。
