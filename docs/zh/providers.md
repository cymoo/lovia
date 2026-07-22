# Provider 与模型

lovia 不绑定任何模型供应商，也没有引入厚重的适配层。两个内置 Provider 通过 `httpx` 直接对接
OpenAI Chat Completions 和 Anthropic Messages；所有 OpenAI 兼容端点均使用 OpenAI Provider；
接入其他供应商时，只需实现一个 `Protocol`，无须继承特定基类。

```python
from lovia import Agent, ModelSettings

agent = Agent(
    name="assistant",
    model="anthropic:<model>",
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
```

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
没有设置时会带设置提示抛错（`required=False` 时返回 `None`）。

## OpenAI Provider

`OpenAIChatProvider(model, *, api_key=None, base_url=None, client=None,
timeout=None, default_headers=None, supports_json_schema=None,
trust_env=None, replay_reasoning=None, official_dialect=None)`

未显式传入时，凭证和端点来自环境变量：`OPENAI_API_KEY`、`OPENAI_BASE_URL`
（默认 `https://api.openai.com/v1`）。

**OpenAI 兼容端点**（DeepSeek、Ollama、vLLM、LM Studio 等）：把
`OPENAI_BASE_URL` 指向服务，模型名不加前缀即可。适配器会按端点调整方言：官方 API 使用
`max_completion_tokens` 和原生 `response_format` JSON schema；兼容端点使用旧的
`max_tokens`，结构化输出默认通过[提示词](structured-output.md#如何向模型提供-schema)实现，
除非你传 `supports_json_schema=True`。只有官方 API 端点缺 API key 才算错误；无 key 的本地
端点可以直接工作。如果方言判断错了（比如官方 API 前面有代理），用 `official_dialect=`
覆盖——方言只关乎请求形状，鉴权仍然跟随真实 host，所以无 key 的网关照常工作。

**Reasoning 模型**（DeepSeek 风格的 `reasoning_content`）：thinking 会作为
[`ReasoningDelta`](streaming.md#模型输出) 事件流出，并保存成 reasoning entry。下一次请求时，
有些端点要求把这些 entry 回放回去（DeepSeek thinking 模型否则会返回 400），而官方 API
拒绝这个字段。所以回放默认按端点决定：`api.deepseek.com` 开启，官方 API 关闭，其他兼容端点开启。
`replay_reasoning=` 可以强制指定行为。只会回放由这个 provider 产出的 entry。

## Anthropic Provider

`AnthropicProvider(model, *, api_key=None, base_url=None, client=None,
timeout=None, anthropic_version="2023-06-01", default_max_tokens=16_384,
default_headers=None, trust_env=None, official_dialect=None)`

未显式传入时，凭证和端点来自环境变量：`ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`
（默认 `https://api.anthropic.com/v1`）。Messages API 每次请求都需要 `max_tokens`，
所以当 `settings.max_tokens` 未设置时，适配器会发送 `default_max_tokens`
（16,384，对齐默认上下文策略的输出预留）。

**Extended thinking**：按 Anthropic API 的方式通过 provider options 开启：

```python
settings = ModelSettings(
    max_tokens=16_000,
    provider_options={
        "anthropic": {"thinking": {"type": "enabled", "budget_tokens": 8_000}}
    },
)
```

thinking 会作为 `ReasoningDelta` 流出；signature 和 `redacted_thinking` block 会原样
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

## 提示词缓存

Provider 缓存能让长 agent 循环的成本变得可控。system prompt 和工具 schema 每轮都会重新发送，
而 lovia 的[压缩会保持 prompt 前缀字节稳定](context.md)，正是为了让它们持续命中缓存。

- **OpenAI**：服务端自动缓存；适配器把 `prompt_tokens_details.cached_tokens` 暴露为
  `usage.cache_read_tokens`。
- **Anthropic**：需要显式开启。按 agent 手动开启后，适配器会在**最后一个 system block
  和最后一个工具定义**上放置 `cache_control: {"type": "ephemeral"}`，作为稳定前缀的断点：

  ```python
  settings = ModelSettings(provider_options={"anthropic": {"cache_system": True}})
  ```

  缓存读写分别暴露为 `usage.cache_read_tokens` / `usage.cache_write_tokens`。

无论哪种方式，`usage.input_tokens` 都是**完整** prompt 大小，包含已缓存 token；cache 字段
只是拆分总量，不额外相加。想受益就要保持前缀稳定：易变的
[动态指令](agents.md#指令)（如时间戳、请求 ID）会导致缓存每轮失效。

## 上下文窗口

上下文窗口不是「模型名」的属性，而是 **(endpoint, model, deployment)** 三元组的属性：
同一个 `qwen2.5`，在这台 vLLM 上是 32K，在那台上就是 4K。所以默认
[`Compaction`](context.md) 策略按一条链来解析它，可信度从高到低：

| 来源 | 何时生效 |
| --- | --- |
| 显式配置 | `Compaction(context_window=…)`——`lovia web` 里也可用 `--context-window` / `LOVIA_CONTEXT_WINDOW` 设置。这是**唯一**面向用户的旋钮；下面各条全部自动 |
| **端点自己的拒绝** | 撞墙一次之后：provider 会在报错里点名上限（`maximum context length is 65536 tokens`） |
| **端点的 `/models` 列表** | vLLM / SGLang 公布 `max_model_len`；Anthropic 官方 API 公布 `max_input_tokens`；Groq、Together、OpenRouter 也各自公布 |
| 内置表 | 按 **host** 索引：`api.openai.com` 上的 OpenAI 别名、`api.anthropic.com` 上的整条 Claude 线、`api.deepseek.com` 上的 DeepSeek |
| — | 都问不到：不做主动压缩，只剩 reactive overflow 兜底 |

只有第二条有资格**覆盖**显式配置，而且只能往下压——那是端点在执行限制，不是猜测。
你在 200K 的模型上配 `100_000`，它仍然是 100K。

表按 host 索引，因为 `gpt-4.1` 是关于 **OpenAI 那个部署**的事实。vLLM 用
`--max-model-len 8192` 重新暴露出来的 `gpt-4.1` 完全是另一回事，而这个适配器同时服务两者
——所以表里没有的 host 不会给出任何窗口，只依赖端点自己报告。

实际效果：一个表里没有的模型，每个 session 的代价是**一次**撞墙。端点点名的窗口随报错
进入 policy 状态并随 session 持久化，之后的每一轮（以及同一 session 之后的每次 run）
都按真实值计算。

两点值得知道：

- **无法提供有效信息时，会跳过 `/models` 探测**：例如策略中已经明确配置窗口，或已知端点不会公布
  （`api.openai.com`）、或者已经问过了。结果（包括 miss）按
  `(endpoint, model)` 在进程内记忆，所以即使 `"vendor:model"` 字符串每次 run 和每次 handoff
  都会重建 provider，一个端点也最多被问一次。探测在首次模型调用前完成，并使用自己的短超时：
  端点慢只会耽误一瞬，不会拖住整个 run。
- **Anthropic 的窗口来自端点本身。** 官方 Models API 按模型公布 `max_input_tokens`，
  探测读到的是**你的组织**实际拿到的窗口——当前一代是 1M，更早的模型是 200K。
  内置表只在探测答不上来时垫底。

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
`ToolCallDelta`、`UsageDelta`、`FinishDelta`、`EntryCompletedDelta`。三个可选
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
| `LOVIA_PROVIDER_TRUST_ENV` | 是否遵守 `HTTP(S)_PROXY` / `NO_PROXY` | 关闭 |
| `LOVIA_HTTP_CA_BUNDLE` | 出站 TLS 自定义 PEM bundle | — |
| `LOVIA_HTTP_INSECURE` | 关闭证书校验 | 关闭 |

构造器参数（`timeout=`、`trust_env=`）优先于环境变量。TLS 校验按顺序解析：
`LOVIA_HTTP_INSECURE` → CA bundle → 安装了可选 `truststore` 包时使用 OS trust store
（`lovia[web]` 会带上）→ `certifi`。同一套解析也覆盖 [`http_fetch` 工具](built-in-tools.md#http-请求)，
所以一个内网 CA 设置可以修复所有出站请求。

**错误分类**会进入[重试机制](retries.md)：HTTP 408/429/5xx 以及传输层超时/断连是
可重试的 `ProviderError`；上下文长度错误会按供应商识别（状态 + 消息关键词），并抛
`ContextOverflowError`，触发 reactive compaction，而不是重试。这个异常还带着
`reported_window`——端点在报错里点名的上限（如果它点了名）。

## 注意事项

- **Ollama 必须显式设 `context_window`。** 它根本不报 overflow：会静默把 prompt 截断到
  `num_ctx`（默认 4096），而且会**从最旧的 token 开始丢弃**——系统提示词和工具定义最先受到影响。
  它的 OpenAI 兼容 `/models` 接口也不公布窗口，因此解析链的每一层都无法探测，且永远
  不会有报错来提醒你。请设置 `Compaction(context_window=…)` 与你的 `num_ctx` 对齐。
- **Anthropic prompt caching 需要显式开启**（`cache_system: True`）。官方 API 上的长循环
  如果不开，每轮都会重新支付完整 prompt。
- **`trust_env` 默认关闭**。这是有意为之，避免环境里的代理设置在无提示时改变路由。在需要代理的
  环境里设置 `LOVIA_PROVIDER_TRUST_ENV=1`，否则不会使用代理。
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
