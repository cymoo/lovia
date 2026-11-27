# 图片发不出去时：拒收恢复与默认看图

> 状态：设计第 3 版（已吸收两轮审阅）· 2026-11-28 · 关闭 [#532](https://github.com/cymoo/lovia/issues/532) · 范围：`lovia/runtime`、`lovia/providers`、`lovia/parts.py`、`lovia/plugins/mcp.py`、`lovia/workspace/tools.py`、`lovia/web`。
>
> 行号指向 `main`（`13bfaeba`，v0.9.302）。

## 问题

一张图进入请求有三条路：用户消息里的 `ImagePart`（Web 附件、SDK 直接传入），工具结果里的图片（`view_image`、MCP），以及插件注入的条目。今天它们有两个毛病。

**被拒就整轮失败。** 提供方拒收一张图时返回 400，没有任何降级或重试，运行以提供方的报错结束（#532）。常见触发：浏览器 MCP 返回 1280×12000 的长截图（Anthropic 单边上限 8000 px）、图片数量超限、文件头和声明的 MIME 不符、模型本身不收图。

**「自动」其实是按地址猜。** 两个内置适配器的 `supports_vision` 默认只对官方地址（`api.openai.com`、`api.anthropic.com`）为真（`openai_chat.py:340`、`anthropic.py:194`），其他地址一律当作看不到图。设置里的提示却写着「按已知模型能力表判断」。结果是：

- 看不到图的模型被手动标成「支持」：附件、`view_image`、MCP 截图都会让运行失败；视觉角色的 `describe_image` 也不会提供（`web/builder.py:223`）。
- 能看图的模型在非官方地址（OpenRouter、通义、GLM 网关、本地 vLLM）：图片全变成 `[image: …]` 占位，`view_image` 被隐藏，模型明明能看却看不到。

现在和今后的模型大多能看图，按地址猜是错的默认值。

## 结论

**默认发图；被拒时把图换成文字占位重发，并在这个对话里记住。** 用户不再需要选择模型是否支持视觉。

分两个 PR：

| PR | 内容 | 关闭 |
| --- | --- | --- |
| 1 · core | 拒收识别、单次调用内的降级重试、按对话记忆、MIME 嗅探、5 MB 按编码长度计、MCP `raw` 不再存 base64 | #532 |
| 2 · 默认看图 | 所有 provider 默认发图、按每次调用筛选工具（新增 `Tool.describes_images`）、`describe_image` 直接调用视觉模型、Web 去掉视觉选项并显示提示 | — |

默认值放在 PR 2，和动态工具一起上线：只改默认值的话，现有 builder 会因为「主模型能看图」而不再构建 `describe_image`，DeepSeek 用户的视觉角色会在两个版本之间消失。

## PR 1：被拒时恢复

### 1. 识别拒收：一个分类函数，不新增异常类

```python
# lovia/providers/_images.py
def image_rejection(exc: BaseException) -> Literal["unsupported", "image"] | None:
    """What a provider's refusal says about the images in its request, or
    None when it is not about images."""
```

- 只看 `ProviderError`，状态码为 `400`、`413` 或 `422`，报错正文（`body`，没有时用 `str(exc)`）提到图片（`image`、`image_url`、`multimodal`、`vision`）。
- **`"unsupported"`**（模型或接口不收图）只认明确的说法：
  - `does not support image(s)` / `vision` / `multimodal`；
  - `image … not supported`（如 `Image input is not supported`）；
  - `not a multimodal model`；
  - `unknown variant image_url`（DeepSeek）。OpenAI Chat 格式里图片只出现在用户消息中（工具结果的图也随附在合成的用户消息里，`openai_chat.py:141`），所以这句等于「不收图」；
  - `image_url` 同时出现时的 `only supported by certain models`（OpenAI 对不收图的模型这样回答）。
- 其余提到图片的诊断都是 **`"image"`**：像素或尺寸超限、解码失败、数量超限、MIME 不符，以及说不清楚的。
- 请求里是否真的带了图由循环判断（见第 3 节）：没带图的请求不走图片恢复。

不新增 `ImageRejectedError`：分类只依赖 `ProviderError` 已有的 `status_code` 和 `body`，第三方 provider 抛的 `ProviderError` 自动适用，公共接口也少一个名字。

**与上下文超限的优先级。** `_http.raise_for_provider_status`（`providers/_http.py:62`）先判超限，`request too large` 这类正文会被当成超限。正文同时有图片和限额证据（`pixel`、`dimension`、`too many image`、`exceed`、`size` 等与图片同现）时，不判为超限，留给图片恢复；笼统的 413 和 token 超限维持现状。408 不参与（它是可重试的网络错误）。

**通用重试放过它。** `stream_model_turn` 的通用重试（`runtime/model_turn.py:296`）遇到带图请求的 `image_rejection(exc)` 时直接抛出，和 `ContextOverflowError` 一样，避免自定义 `retry_on` 先把原图重发一遍；不带图的请求照旧由重试策略决定。正文同时提到 token 或上下文窗口时，仍按上下文超限处理（「图文 token 合计超出上下文窗口」应当压缩）。

### 2. 降级：在发给模型的视图里原位替换

降级只改这次请求的视图，transcript 和 Session 不变（与上下文压缩同一原则）。被选中的 `ImagePart` 原位换成 `TextPart(project_parts([part]))`：

- 用户消息：`InputEntry.content` 列表里换，文字、顺序、其他图片都保留。
- 工具结果：`ToolResultEntry.parts` 里换，`output` 不动。只删图片不行：`view_image` 返回 `[路径, 图片]`（`workspace/tools.py:300`），Anthropic 在 `parts` 非空时用 `parts` 编码 `tool_result.content`（`anthropic.py:721`），删掉图片后只剩路径，占位就丢了。

已记住的决定在交给上下文策略之前就应用到条目上，所以策略的估算、摘要和随后的校准看到的都是实际发出的内容。这次调用里新作的决定应用在视图构建的最后一步（压缩、插件注入之后），每次构建视图都重新应用，所以同一次调用里因上下文超限重新压缩时，刚去掉的图不会被送回去；这样的调用不参与 token 校准（策略估算的是带图的视图）。

### 3. 单次模型调用内的恢复

一次模型调用（`_model_phase`）内：

| 拒收 | 第一次重试 | 第二次重试 |
| --- | --- | --- |
| `"unsupported"` | 去掉视图里的全部图片 | 无：再失败就报错 |
| `"image"` | 只去掉「新增」图片；没有新增图片，或新增就是全部时，去掉全部 | 上一步还剩图时，去掉全部 |

- **前提**：这次调用还没有任何输出流给调用方（与超限恢复相同）；视图里确实有图。去掉后没有图可去时，直接抛出原错误。
- **新增图片**：transcript 中位于模型最后一次输出（助手文字、工具调用、推理）之后的条目里的图，以及映射不回 transcript 的图（插件注入）。那次输出之前的图都随那次成功调用发出过。这个位置直接从 transcript 读出，所以从检查点恢复的运行也知道。常见情形「用户先前传过设计稿，现在一张新截图超限」只会去掉新截图。
- 每次重试都会少发至少一张图，所以一次调用最多两次图片重试。
- 重试复用已构建的视图，不重新压缩、不重新执行工具。上下文超限的一次恢复照旧，两种恢复可在同一次调用里先后发生。
- 重试之前发出 `ImagesWithheld` 事件（第 5 节）。

### 4. 只记住恢复成功的结论，按对话保存

保存在 runner 的运行状态里（`context_state` 下的独立键 `images`），不归上下文策略管：runner 把整个 `context_state` 原样存进检查点和 Session 片段的 `meta`（`context/policy.py:176`、`loop.py:704`），所以关掉压缩（`NoopContextPolicy`）也能跨轮记住。

```json
{"images": {"blind": ["<endpoint>\u0000<model>"],
            "withheld": [[12, 1, "3f2a…"], [15, 0, "9c0d…"]]}}
```

- **`blind`**：明确不收图的「端点 + 模型」（沿用 `context/state.py:309` 的 `window_key`）。之后对这个模型的请求从一开始就去掉全部图片。只由 `"unsupported"` 写入，而且只记模型、不记位置：对话换了模型（切换、handoff）后，那些图照样发给新模型。说图片格式的报错（`Image format is not supported`、`does not support image/gif`）属于 `"image"`。
- **`withheld`**：恢复成功那次被去掉的历史图片位置：`[条目序号, part 序号, 指纹]`。条目序号相对于去掉系统提示后的 transcript（即 Session 条目的序号），在 append-only 的 Session 里稳定；指纹由图片数据的长度和首尾片段（URL 图片用 URL）算出，用来确认那个位置上还是同一张图。撤回、编辑消息或 handoff 让位置错开时，指纹对不上就不生效。
- **只在恢复后的那次调用成功时写入。** 重试仍失败时不写，运行照原样报错。
- **不建「坏图名单」。** 内容哈希会把好图永久屏蔽：端点最多收 20 张图时，第 21 张让请求被拒，它本身没有问题；记录的是位置，用户之后单独重新发同一张图，它是新位置上的新图，照样能看。URL 图片也不必定义身份。
- 映射不回 transcript 的图（插件注入）只在当次调用里去掉，不写入。映射按「条目 + part 序号」，同一个 `ImagePart` 对象出现两次就是两张图。
- 不记录「模型能看图」的正面证据：默认就当能看（PR 2）；一次 HTTP 成功也不能证明网关真的把像素交给了模型。

### 5. `ImagesWithheld` 事件

```python
@dataclass
class ImagesWithheld(ContextEvent):
    count: int         # 这次重试去掉的图片张数
    unsupported: bool  # 提供方说这个模型不收图
```

在重试之前发出，只针对新的决定（之后按记忆去掉的图不再发）。Web 据此显示一行提示（PR 2）：「本次有 N 张图片改用文字占位」，不声称图片损坏；`unsupported` 时提示可以在「角色指派」里配一个视觉模型代看。提示只在当前页面显示，刷新后不恢复。

### 6. 发送前就能避免的拒收

- **MIME 嗅探。** 新增 `parts.sniff_image_mime(data: bytes) -> str | None`，按文件头识别 PNG、JPEG、GIF、WebP。`view_image`、MCP 图片、Web 附件、`describe_image` 和 `ImagePart.from_path` 都用文件头的结果，扩展名只用来决定要不要读；文件头不是这四种时不当图片发送。
- **5 MB 按编码长度计。** 提供方的单图上限按 base64 字符串计（Anthropic 文档）。`VIEW_IMAGE_MAX_BYTES` 改为 5 MiB 的 base64 对应的原始字节数（3.75 MiB），`view_image`、`describe_image` 和 Web 附件共用；MCP 已按编码长度计（`plugins/mcp.py:193`）。

### 7. MCP 结果的 `raw` 不再存 base64

没能转成可发送图片的 MCP 结果（格式不支持、超限、`isError`、自定义渲染函数）以 `MCPToolResult` 留在 `ToolResultEntry.raw`；渲染文字很短，`_KEEP_RAW_MAX_CHARS` 不会丢掉它，序列化时整段 base64 进了 Session 和检查点。给 `MCPToolResult` 加 `model_dump()`（`to_json_safe` 优先用它，`transcript.py:309`），存储时把 `data`、`blob` 换成渲染时的同一段占位。内存里的结果、`ToolCallCompleted` 事件和自定义渲染函数拿到的仍是完整对象。

## PR 2：默认看图

### 1. 视觉能力只有两种状态 ⚠️ breaking

- 所有 provider 默认 `supports_vision=True`，包括没有声明这个属性的第三方实现（`providers/base.py:203`）。
- 显式 `False` 由循环在视图里去掉全部图片（第 2 节的同一函数），覆盖 SDK 传入的用户图片；不再由适配器的 `include_images` 只管工具结果，适配器给什么编码什么。
- `config.json` 的 `vision`：`on` 与 `auto` 同义（发图），`off` 从不发图。编辑模型的其他字段时保留原值。

### 2. 按每次调用提供工具 ⚠️ breaking

- 新增 `Tool.describes_images`（与 `returns_images` 对称）。当前模型看得到图时提供 `returns_images` 的工具、不提供 `describes_images` 的工具；看不到时（显式 `off`，或本对话记住了 `blind`）反过来。
- 这条规则对显式传入 `agent.tools` 的工具同样生效：今天显式工具从不筛选（`tools/base.py:146`），这一点改掉，规则只有一条。
- 执行查找表（`tools_by_name`，`runtime/tool_calls.py:161`）始终包含全部工具，每次调用只筛选发给模型的工具列表，所以发给模型的永远是可执行工具的子集。
- `"unsupported"` 的重试会按刚记住的 `blind` 重新筛选工具：DeepSeek 配了视觉角色时，第一次拒收后的那次重试里就能调用 `describe_image`，用户不用再问一轮。

### 3. `describe_image`

- 只要配置了视觉角色且该角色不是显式 `off`，就构建；是否提供由第 2 节按每次调用决定。
- 直接调用视觉 provider（一次 `stream`，不跑 `Runner`）。视觉模型也拒收图片时直接返回工具错误，不会对着占位符生成一段「描述」。

### 4. Web

- 模型表单去掉「视觉能力」选项，列表去掉「视觉」标记，删掉错误的提示文案。
- 附件除显式 `off` 外一律内嵌发送；已知 `blind` 的由循环降级。
- 显示 `ImagesWithheld` 提示。

## 代价与边界

- 对不收图的模型，每个对话的第一张图多一次很快失败的请求。
- 同一条历史图片的降级一旦确定，之后的请求前缀保持稳定；首次降级和工具列表变化的那次会改变前缀，提示缓存失效一次。
- 误判：把无关的 4xx 当成图片问题，最坏是图片被降级、运行继续；拒收报错没认出来，和今天一样这一轮失败。
- 第二次重试去掉全部图片时，之前成功发过的好图也会在之后的请求里改为占位。用户重新附上即可再看。

## 验收（合并前）

1. 端点最多收 20 张图，第 21 张触发拒收后，用户单独重发这张图，模型仍能看到。
2. Anthropic 下 `view_image` 的「路径 + 图片」降级后，请求里路径和占位都在。
3. DeepSeek 首次拒图后，同一次提问里由视觉角色代看成功；视觉角色自己也拒图时，返回明确的工具错误。
4. 同一端点开两个对话，一个已降级，另一个仍能独立试发图片。
5. 同一次调用先遇图片拒收、再遇上下文超限（或反过来），重试的请求不含已去掉的图。
