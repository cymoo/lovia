# Provider 与模型

lovia 内置 OpenAI Chat Completions 和 Anthropic Messages 适配器，也支持相应的兼容端点。
接入其他协议时，实现 `Provider` Protocol 即可，无须继承基类。

```python
from lovia import Agent, ModelSettings

agent = Agent(
    name="assistant",
    model="anthropic:<model>",
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
```

先按端点协议选择接入方式：

| 端点 | `model` 写法 | 主要环境变量 |
| --- | --- | --- |
| OpenAI 官方或兼容协议 | `"<model>"` | `OPENAI_API_KEY`、`OPENAI_BASE_URL` |
| Anthropic 官方或兼容协议 | `"anthropic:<model>"` | `ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL` |
| 自定义协议 | 传入 `Provider` 实例 | 由实现决定 |

## 模型字符串

`Agent(model=...)` 接受 `"vendor:model"` 字符串或 `Provider` 实例。

| 前缀 | Provider | 别名 |
| --- | --- | --- |
| `openai:` | OpenAI Chat Completions | `oai:`, `openai-chat:` |
| `anthropic:` | Anthropic Messages | `claude:` |
| （无） | OpenAI Chat Completions | — |

**不带前缀的模型名**（如 `"endpoint-model"`）会走 OpenAI-compatible Provider。这是
`OPENAI_BASE_URL` 服务的推荐写法。有一个保护：模型名如果不带前缀且以 `claude` 开头，会记录 warning 日志，
因为这几乎总是漏了 `anthropic:` 前缀。lovia 不设默认模型；没有模型就运行 agent
会抛 `UserError`。

为了避免在脚本里写死模型，`model_from_env()` 会读取 `LOVIA_MODEL`（唯一入口）；
没有设置时会抛出带配置提示的错误；传入 `required=False` 时则返回 `None`。

## OpenAI Provider

`OpenAIChatProvider(model, *, api_key=None, base_url=None, client=None,
timeout=None, default_headers=None, supports_json_schema=None,
trust_env=None, replay_reasoning=None, official_dialect=None,
supports_vision=None, extra_body=None)`

未显式传入时，凭证和端点来自环境变量：`OPENAI_API_KEY`、`OPENAI_BASE_URL`
（默认 `https://api.openai.com/v1`）。

**OpenAI 兼容端点**（DeepSeek、Ollama、vLLM、LM Studio 等）：把
`OPENAI_BASE_URL` 指向服务，模型名不加前缀即可。适配器会按端点调整方言：官方 API 使用
`max_completion_tokens` 和原生 `response_format` JSON schema；兼容端点使用旧的
`max_tokens`，结构化输出默认通过[提示词](structured-output.md#如何向模型提供-schema)实现，
除非你传 `supports_json_schema=True`。只有官方 API 端点缺 API key 才算错误；无 key 的本地
端点可以直接工作。如果方言判断错了（比如官方 API 前面有代理），用 `official_dialect=`
覆盖——方言只关乎请求形状，鉴权仍然跟随真实 host，所以无 key 的网关照常工作。

**Reasoning 模型**（DeepSeek 风格的 `reasoning_content`，或 vLLM、OpenRouter 用的
`reasoning` 字段）：thinking 会作为 [`events.ReasoningDelta`](streaming.md#模型输出) 事件流出，
并保存成 reasoning entry。下一次请求时，
有些端点要求把这些 entry 回放回去（DeepSeek thinking 模型否则会返回 400），而官方 API
拒绝这个字段。所以回放默认按端点决定：`api.deepseek.com` 开启，官方 API 关闭，其他兼容端点开启。
`replay_reasoning=` 可以强制指定行为。只会回放由这个 provider 产出的 entry。

**图片**：`supports_vision=` 声明模型是否接受图片输入。默认（`None`）只在官方 API 上认为支持，
因为兼容端点背后的模型无法从名字判断。`OPENAI_BASE_URL` 后面是视觉模型时，传 `True`。
lovia 通过 `lovia.providers.supports_vision(provider)` 读取这个标志（没有这个标志的 provider
视为不支持）：Web UI 只在它为真时把附件图片直接发给模型，否则只给模型文件路径。

## Anthropic Provider

`AnthropicProvider(model, *, api_key=None, base_url=None, client=None,
timeout=None, anthropic_version="2023-06-01", default_max_tokens=16_384,
default_headers=None, trust_env=None, official_dialect=None,
supports_vision=None, extra_body=None)`

未显式传入时，凭证和端点来自环境变量：`ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`
（默认 `https://api.anthropic.com/v1`）。Messages API 每次请求都需要 `max_tokens`，
所以当 `settings.max_tokens` 未设置时，适配器会发送 `default_max_tokens`
（16,384，对齐默认上下文策略的输出预留）。`supports_vision=` 的用法同[上文](#openai-provider)：
官方端点上默认为真，`ANTHROPIC_BASE_URL` 后面默认为假。

**Extended thinking**：按 Anthropic API 的方式通过 provider options 开启：

```python
settings = ModelSettings(
    max_tokens=16_000,
    provider_options={
        "anthropic": {"thinking": {"type": "enabled", "budget_tokens": 8_000}}
    },
)
```

thinking 会作为 `events.ReasoningDelta` 流出；signature 和 `redacted_thinking` block 会原样
往返。回放也按端点处理：官方 API 会拒绝没有开启 thinking 的请求里携带 thinking
block，所以官方 API 会剥掉陈旧 block；而默认会思考的兼容端点（如 DeepSeek 的
`/anthropic` 方言）会始终收到回放。

**Anthropic 方言端点**：DeepSeek 和其他服务可能暴露 Anthropic Messages 方言；把
`ANTHROPIC_BASE_URL` 指过去，上面的兼容处理会自动生效。

## 多供应商故障转移

一个 Agent 只连接一个 Provider；lovia 不提供进程内的故障转移链。瞬时错误交给
[重试策略](retries.md)。供应商级的故障转移，应将 `base_url` 指向一个
路由网关（LiteLLM、OpenRouter 等），由它在 server side 完成切换——这样一次运行始终只有
一个端点、一个上下文窗口、一套能力集。另外 session 跨 run 持久化：失败的请求随时可以换个
模型对同一 session 重跑。

## ModelSettings

采样参数会转交给 provider；`None` 表示“不发送”，由 provider 使用默认值。

| 字段 | 发送形式 |
| --- | --- |
| `temperature` | 原样 |
| `top_p` | 原样 |
| `max_tokens` | 官方 OpenAI 用 `max_completion_tokens`，其他端点用 `max_tokens` |
| `stop` | `stop`（OpenAI）/ `stop_sequences`（Anthropic） |
| `parallel_tool_calls` | OpenAI 按原样发送；Anthropic 使用 `disable_parallel_tool_use` tool-choice，是 [`Tool.parallel`](tools.md#并发执行与屏障) 在请求侧的对应项 |
| `provider_options` | 供应商键控的额外参数，见下 |

**`provider_options`** 是供应商特有参数的扩展入口，不需要等框架发版：它是一个按 vendor
分组的 dict，内容会原样合并进请求体。

```python
ModelSettings(provider_options={
    "openai": {"logprobs": True},
    "anthropic": {"thinking": {"type": "enabled", "budget_tokens": 4_000}},
})
```

适配器读取自己的 key：`"openai"` 再 `"openai-chat"`，或 `"anthropic"` 再
`"claude"`，后面的 key 覆盖前面的；值为 `None` 会**移除**适配器本来要发送的字段
（例如 `{"stream_options": None}`）。

### `extra_body`：连接级的请求字段

同样的原始字段也可以绑在 *provider* 上而不是 agent 上：

```python
provider = OpenAIChatProvider(
    "qwen3.8-flash-next",
    base_url="http://l40:8000/v1",
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)
```

`extra_body`（`provider_from_string` 也接受这个关键字，`supports_vision` 同样）放的是*端点*永远需要的东西——vLLM
部署的模板开关、推理强度、网关的 `service_tier`——并且会到达经过这个 provider 的每一次调用：
共用它的每个 agent，以及自建 settings 的上下文压缩摘要器。`provider_options` 仍是按 agent
的旋钮。两者语义相同，一个请求按固定顺序渲染：

1. `ModelSettings` 的类型化字段，
2. `extra_body`，
3. 本适配器的 `provider_options`，

后一层覆盖前一层，只看顶层 key——`chat_template_kwargs` 这样的嵌套对象整体替换。所以原始的
`max_tokens` 胜过类型化的那个，agent 也能关掉连接打开的思考。唯一的例外是 Anthropic 适配器的
`output_config`：它同时装着结构化输出的 `format` 和用户的 `effort`，extras 里的对象会与之合并
而不是替换，`None` 仍然删除整个字段。字段原样合并——不会翻译成端点的方言（官方 OpenAI API 要
`max_completion_tokens`，兼容端点要 `max_tokens`，写错会被拒绝）。`DEBUG` 级别下适配器只记录
原始字段名和来源，不记录值。

`lovia web` 的界面把 `extra_body` 作为每个模型档案的*额外请求参数*暴露出来；各家推理参数的
写法见[模型配置与切换](web-ui.md#模型配置与切换)。

## 提示词缓存

Provider 缓存能让长 agent 循环的成本变得可控。system prompt 和工具 schema 每轮都会重新发送，
而 lovia 的[压缩会保持 prompt 前缀字节稳定](context.md)，正是为了让它们持续命中缓存。

- **OpenAI**：服务端自动缓存；适配器把 `prompt_tokens_details.cached_tokens` 暴露为
  `usage.cache_read_tokens`。
- **Anthropic**：需要显式开启。按 agent 手动开启后，适配器会在三个稳定边界上放置
  `cache_control: {"type": "ephemeral"}` 断点：**最后一个工具定义**、**最后一个 system
  block**，以及**对话里最后一个稳定的块**。这样每一轮都能从缓存读取之前的全部历史，
  而不必重新付费：

  ```python
  settings = ModelSettings(provider_options={"anthropic": {"cache_system": True}})
  ```

  历史断点放在最后一个工具结果或助手块上，不会放在末尾的用户文本上：新的用户消息，以及
  插件只为本次调用注入的提醒（待办列表、运行中的子 agent）都排在它之后，因此它标记的
  前缀在下一次请求里逐字节不变。缓存读写分别暴露为 `usage.cache_read_tokens` /
  `usage.cache_write_tokens`。

  断点默认使用 API 的 5 分钟 TTL，每次读取都会刷新它；只要各轮紧挨着发生就够用。
  如果是聊天场景，用户两条消息之间停顿较久，每次回来都要为整段历史重新付费；
  `cache_ttl: "1h"` 让缓存保持一小时（同时隐含开启 `cache_system`）：

  ```python
  settings = ModelSettings(provider_options={"anthropic": {"cache_ttl": "1h"}})
  ```

  1 小时缓存的写入价格是基础输入价的 2×（5 分钟为 1.25×），因此只有当共享前缀的
  相邻两轮间隔在 5–60 分钟之间时才划算。兼容网关不一定接受 `ttl` 字段。

无论哪种方式，`usage.input_tokens` 都是**完整** prompt 大小，包含已缓存 token；cache 字段
只是拆分总量，不额外相加。想受益就要保持前缀稳定：易变的
[动态指令](agents.md#编写指令)（如时间戳、请求 ID）会导致缓存每轮失效。

## 上下文窗口

上下文窗口不是「模型名」的属性，而是 **(endpoint, model, deployment)** 三元组的属性：
同一个 `qwen2.5`，在这台 vLLM 上是 32K，在那台上就是 4K。所以默认
[`Compaction`](context.md) 策略按一条链来解析它，可信度从高到低：

| 来源 | 何时生效 |
| --- | --- |
| 显式配置 | `Compaction(context_window=…)`——`lovia web` 里是模型配置中的上下文窗口。这是**唯一**面向用户的旋钮；下面各条全部自动 |
| **端点自己的拒绝** | 撞墙一次之后：provider 会在报错里点名上限（`maximum context length is 65536 tokens`） |
| **端点的 `/models` 列表** | vLLM / SGLang 公布 `max_model_len`；Anthropic 官方 API 公布 `max_input_tokens`；DeepSeek、Groq、Together、OpenRouter 也各自公布 |
| — | 都问不到：不做主动压缩，只剩 reactive overflow 兜底 |

只有第二条有资格**覆盖**显式配置，而且只能往下压——那是端点在执行限制，不是猜测。
你在 200K 的模型上配 `100_000`，它仍然是 100K。

Lovia 不内置「模型名→窗口」表。这种表每次模型发布都会过时，而且模型名不等于部署：vLLM 用
`--max-model-len 8192` 重新暴露出来的 `gpt-4.1`，和 OpenAI 的 `gpt-4.1` 完全是两回事。

实际效果：端点不公布窗口的模型（主要是 OpenAI 官方 API），每个 session 的代价是**一次**撞墙。
重建的 prompt 会按报错里点名的窗口计算，所以即使历史很长，这一次重试也能放得下。之后这个窗口
进入 policy 状态并随 session 持久化，之后的每一轮（以及同一 session 之后的每次 run）都按真实值
计算。配置 `context_window` 可以连这一次也省掉。

两点值得知道：

- **无法提供有效信息时，会跳过 `/models` 探测**：例如策略中已经明确配置窗口，或已知端点不会公布
  （`api.openai.com`）、或者已经问过了。结果（包括 miss）按
  `(endpoint, model)` 在进程内记忆，所以即使 `"vendor:model"` 字符串每次 run 和每次 handoff
  都会重建 provider，一个端点也最多被问一次。完全没拿到回答的探测（连不上、超时、429、5xx）
  不算 miss，几分钟后会再问。探测在首次模型调用前完成，并使用自己的短超时：端点慢只会耽误一瞬，
  不会拖住整个 run。摘要模型如果用自己的 provider，会在第一次折叠时以同样方式探测。
- **Anthropic 的窗口来自端点本身。** 官方 Models API 按模型公布 `max_input_tokens`，
  探测读到的是**你的组织**实际拿到的窗口——当前一代是 1M，更早的模型是 200K。
  探测直接查询单个模型（`GET /models/{model}`），所以别名也能解析。

## 自定义 Provider

Provider 是一个 `Protocol`，四个成员，没有基类：

```python
class Provider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def model(self) -> str | None: ...
    @property
    def supports_json_schema(self) -> bool: ...
    def stream(
        self, entries, *, tools=None, response_format=None, settings=None
    ) -> AsyncIterator[ModelDelta]: ...
```

`stream` 接收 transcript view，类型是 `TranscriptEntry`（比 chat message 丰富，保留
reasoning 和元数据），并产出 `ModelDelta`：`TextDelta`、`ReasoningDelta`、
`ToolCallDelta`、`UsageDelta`、`FinishDelta`、`EntryCompletedDelta`，都从 `lovia.transcript`
导入。其中两个和流事件同名，但不是同一个类型：provider 产出的是
`lovia.transcript.TextDelta(text=...)`，runner 把它转换成流的使用方看到的
`lovia.events.TextDelta(delta=...)`（`ReasoningDelta` 同理）。三个可选
protocol 能让自定义 provider 在压缩中更接近内置 provider 的体验：

```python
class ContextWindowProvider(Protocol):     # 本地报告窗口，不发请求
    def context_window(self) -> int | None: ...

class ContextWindowDiscovery(Protocol):    # 异步、一次性地向端点查询
    async def discover_context_window(self) -> int | None: ...

class TokenEstimator(Protocol):            # 比启发式更准的 token 计数
    def estimate_tokens(self, entries) -> int: ...
```

它们是 `runtime_checkable` 的，只检查方法**存在**——签名写错要到调用时才报错。
`lovia.testing` 里的 [`ScriptedProvider`](testing.md) 是完整、易读的参考实现。

注册一个字符串前缀：

```python
from lovia.providers import register_provider

register_provider("mistral", lambda model: MistralProvider(model=model))
agent = Agent(name="x", model="mistral:large-3")
```

也可以通过包的 `lovia.providers` entry-point group 发布；前缀会在首次使用时懒加载。
entry point 不能覆盖内置的 `openai:` / `anthropic:` 前缀（安装包不应在无提示时改变路由）；
显式调用 `register_provider` 则可以。

## 网络：超时、代理、TLS

两个适配器共用一层 HTTP 配置：

| 环境变量 | 作用 | 默认 |
| --- | --- | --- |
| `LOVIA_PROVIDER_TIMEOUT` | 请求超时，秒 | `300` |
| `LOVIA_HTTP_TRUST_ENV` | 是否经本机代理（`0` 关闭） | 开启 |
| `LOVIA_HTTP_CA_BUNDLE` | 出站 TLS 自定义 PEM bundle | — |
| `LOVIA_HTTP_INSECURE` | 关闭证书校验 | 关闭 |

构造器参数（`timeout=`、`trust_env=`）优先于环境变量。（0.9.290 之前代理开关叫
`LOVIA_PROVIDER_TRUST_ENV`，只管 provider；MCP 和 `web_fetch` 会自行读取环境中的代理。）
TLS 校验按顺序解析：
`LOVIA_HTTP_INSECURE` → CA bundle → 安装了可选 `truststore` 包时使用 OS trust store
（`lovia[web]` 会带上）→ `certifi`。

`LOVIA_HTTP_*` 覆盖 lovia 建的所有 HTTP 客户端：provider、[HTTP 方式的 MCP](mcp.md)、
[URL 读取等工具](built-in-tools.md#读取-url)和 embedding。所以一处内网 CA 或代理设置就能
覆盖所有出站请求。自己的代码可以从 `lovia.http_config.client_options()` 拿到同一套选项：

```python
httpx.AsyncClient(timeout=30, **client_options())
```

请求的路由和浏览器一致：设了 `HTTP(S)_PROXY` 就用它，否则用系统代理（Windows 注册表、macOS
设置）；代理绕过列表里的主机（`NO_PROXY`、注册表的 `ProxyOverride`、macOS 的例外）和本机地址直连。
所以浏览器内外网都能访问的网络里，lovia 也都能访问。PAC 文件不读：只靠 PAC 配置的机器上请求会
直连，这时请写明代理（`HTTPS_PROXY=http://proxy:8080 NO_PROXY=.corp.example`）。是否绕过的判断
会缓存，改了绕过列表要重启。

请求失败、而本机又配置了代理时，报错的 hint 会说明 lovia 有没有用它：要么是
"skipped the proxy … since trust_env is off"（没走代理），要么是
"went through the proxy …"（走了代理，常见于代理访问不到、应当加进绕过列表的地址）。

**错误分类**会进入[重试机制](retries.md)：HTTP 408/429/5xx 以及传输层超时/断连是
可重试的 `ProviderError`；上下文长度错误会按供应商识别（状态 + 消息关键词），并抛
`ContextOverflowError`，触发 reactive compaction，而不是重试。这个异常还带着
`reported_window`——端点在报错里点名的上限（如果它点了名）。

## 使用建议

- **Ollama 必须显式设 `context_window`。** 它根本不报 overflow：会静默把 prompt 截断到
  `num_ctx`（默认 4096），而且会**从最旧的 token 开始丢弃**——系统提示词和工具定义最先受到影响。
  它的 OpenAI 兼容 `/models` 接口也不公布窗口，因此解析链的每一层都无法探测，且永远
  不会有报错来提醒你。请设置 `Compaction(context_window=…)` 与你的 `num_ctx` 对齐。
- **Anthropic prompt caching 需要显式开启**（`cache_system: True`）。官方 API 上的长循环
  如果不开，每轮都会重新支付完整 prompt，包括全部历史。这个开关的名字早于历史断点，
  现在它同时覆盖工具、system 和对话历史。
- **残留的系统代理会让所有请求失败。** 代理软件退出了、系统代理却没关，lovia 的请求和浏览器
  一样会失败，报错会写出代理地址。关掉它，或设置 `LOVIA_HTTP_TRUST_ENV=0`（也可以对单个客户端
  传 `trust_env=False`）。注意：只设 `NO_PROXY` 会让系统代理整个失效，因为 Python 这时只读环境变量。
- **由字符串构造的 provider 归运行管理；你传入的实例归你管理。** 你手动构造并传入的
  `Provider` 不会被 runner 关闭；可以跨运行复用，也请自己关闭。
- **`supports_json_schema` 推断跟着端点走。** 兼容端点如果确实支持原生 JSON schema，
  需要显式传构造器参数，才会走原生接口。
- **Anthropic 的 server tools 可能把 turn 暂停。** 通过 `provider_options` 启用 server
  tool（web search、code execution）后，长 turn 可能以 `stop_reason: "pause_turn"` 结束——
  这是 API 要求"重发对话以继续"的信号。lovia 目前不会自动续传：该 turn 以已有的部分内容
  结束，原始 `pause_turn` 会原样透传到 finish reason，调用方可据此识别。

## 延伸阅读

- [结构化输出](structured-output.md)：原生 schema 与 prompt 路径
- [Provider 重试](retries.md)：临时故障处理
- [上下文管理](context.md)：窗口与缓存如何互相配合
- 示例：[`09_model_settings.py`](../../examples/09_model_settings.py)，
  [`10_custom_provider.py`](../../examples/10_custom_provider.py)
