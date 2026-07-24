# 内置工具

内置工具不会自动挂载到 Agent 上。每项内置能力都必须显式导入，因此只需查看 Agent 的
构造代码，就能清楚知道它具备哪些能力。

```python
from lovia import Agent
from lovia.tools import duckduckgo_search, http_request, now, read_page

agent = Agent(
    name="researcher",
    model="<model>",
    tools=[read_page, http_request, duckduckgo_search(), now],
)
```

文件和 Shell Tool 来自[工作区](workspace.md)。操作员输入 Tool 见下方[询问人工](#询问人工)。

## 读取网页

`lovia.tools.read_page` 读取网页的**内容**：HTML 会转成 Markdown，标题层级、列表、
代码块、表格、链接和图片都会保留下来。结果里的
`[the guide](https://example.com/guide)` 是模型下一步可以直接访问的真实 URL —— 而这
正是把网页压平成纯文本会丢掉的东西。

模型看到三个参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `url` | 必填 | 只允许绝对 `http://` / `https://` URL |
| `images` | `False` | 同时返回页面引用的所有图片 |
| `offset` | `0` | 从这个字符偏移处继续读一个长页面 |

其余都是运维层面的决定，因此放在 `page_reader()` 工厂上，而不是每次调用都让模型
为这些参数的 schema 付 token：

```python
from lovia.tools import HttpReader, page_reader

tool = page_reader(                     # 或直接用现成的 read_page
    HttpReader(timeout=15.0, max_bytes=2_000_000, cache_ttl=60),
    max_chars=40_000,
)
```

### 模型看到的结果

```
Title: Example Domain
URL: https://example.com/guide
HTTP 200 · text/html

# Example Domain

This domain is for use in illustrative examples. See
[more information](https://www.iana.org/domains/example).

[... truncated. Continue with offset=20000.]

Images (2):
1. https://example.com/img/hero.png — Hero banner
2. https://example.com/social.png
```

截断说明里直接带上了续读的 offset，所以长页面不再是死路。`read_page` 返回的是
`Page` dataclass（最终 URL、状态码、标题、Markdown 正文、图片列表）；上面这段是它的
结果渲染器给模型和 Web UI 看到的文本。

### 图片

内联 Markdown 本身已经带上了每个 `<img src>`。`images=True` 额外给出一份去重、
绝对化的清单，并覆盖 Markdown 表达不了的来源：`srcset` 中最大的候选、
`<picture><source>` 以及 `og:image`。相对 URL 会依据 `<base href>` 和重定向后的
URL 解析；`data:`、`javascript:` 和纯锚点会被丢弃 —— 一张内联 base64 图片能在单个
属性里塞进 100 KB，而模型对它也无能为力。

### 不执行 JavaScript —— 换后端即可

`HttpReader` 只发一次普通 HTTP 请求，用标准库解析。它不做客户端渲染，所以纯前端
渲染的单页应用可能几乎返回空白。`PageReader` 就是扩展点（形状与 `WebSearch`
协议一致）：

```python
class PageReader(Protocol):
    async def read(self, url: str, *, images: bool = False) -> Page: ...
```

接入一个托管的抽取服务只需要一个短类 —— 这里是直接返回 Markdown 的 Jina Reader：

```python
import httpx
from lovia.tools import Page, page_reader

class JinaReader:
    async def read(self, url: str, *, images: bool = False) -> Page:
        headers = {"X-Return-Format": "markdown"}
        if images:
            headers["X-With-Images-Summary"] = "true"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"https://r.jina.ai/{url}", headers=headers)
        return Page(
            url=url,
            status_code=resp.status_code,
            media_type="text/markdown",
            text=resp.text,
        )

agent = Agent(name="x", model="<model>", tools=[page_reader(JinaReader())])
```

按字符预算裁剪始终是工具层的职责，所以后端应当返回完整正文。

### 缓存与上限

响应按 URL 缓存 `cache_ttl` 秒（默认 300，最多保留 `cache_size` 条）。这主要是为了
正确性：否则用 `offset` 续读会重新下载页面，可能把两个不同版本拼在一起。下载有
1 MB 硬上限；触顶时会设置 `size_capped` 并在结果里说明，因为任何 `offset` 都到不了
那段从未下载的尾部。`4xx`/`5xx` 的正文限制在 500 字符 —— 一个错误页模板不值一整份
预算。

## HTTP 请求

`lovia.tools.http_request` 是给 REST API 和非 HTML 端点用的诚实 HTTP 客户端。它不解释
HTML —— 那是 `read_page` 的职责。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `url` | 必填 | 只允许绝对 `http://` / `https://` URL |
| `method` | `"GET"` | 任意 HTTP 方法 |
| `headers` | `None` | 可选请求头（要覆盖默认 UA 也在这里） |
| `body` | `None` | 对 POST/PUT/PATCH 以 JSON 发送 |
| `timeout` | `30.0` | 秒，范围 1–120 |
| `max_chars` | `20_000` | 结果上限，100–200,000 |

结果由状态行、响应头和正文组成：JSON 紧凑地重新序列化，文本原样通过，二进制只返回
元数据。响应头会带上，因为限流信息和 `Link:` 分页往往正是调用它的目的 —— 但
`set-cookie` 会被隐去，否则会把会话令牌写进转录。下载有 1 MB 上限，正文按
`max_chars` 剪裁并带明确说明。工具会跟随重定向，最终 URL 与请求的不同时会报出来。
TLS 遵守 [`LOVIA_HTTP_*` 设置](providers.md#网络超时代理tls)。

> **没有 SSRF 过滤。** 两个工具都会请求主机能访问到的任何地址，包括私有和内网地址；
> 重定向也可能跳到那里。当模型会接触不可信输入时，请给工具加门禁或隔离网络。

如果只想给可能造成改动的请求加审批，传入内置的谓词。它不是默认值，因为审批是
失败即拒绝的：没有配置审批处理器的 `Runner.run` 调用方会让每个 POST 都被拒掉。

```python
import dataclasses
from lovia.tools import http_request, writes_need_approval

# GET / HEAD / OPTIONS 直接放行，其余先问。
gated = dataclasses.replace(http_request, needs_approval=writes_need_approval)
```

## Web 搜索

`lovia.tools.search` 提供可插拔的搜索工具，自带两种后端：DuckDuckGo 无需 API 密钥，
但要安装 `ddg` extra；Tavily 无需安装额外依赖，但要设置 `TAVILY_API_KEY` 或传入
`api_key=`。

```bash
pip install "lovia[ddg]"   # 仅 DuckDuckGo 后端需要
```

```python
from lovia.tools.search import duckduckgo_search, tavily_search, web_search

tools = [duckduckgo_search()]            # 无需 API 密钥，需要 lovia[ddg]
tools = [tavily_search()]                # Tavily API，读取 TAVILY_API_KEY
tools = [web_search(MySearchBackend())]  # 或你自己的后端
```

工具默认名为 `web_search`（可用 `name=` 覆盖），接收 `query`、`max_results`
（1–20，默认 5）和可选的时间范围过滤 `time_range`（`"d"` / `"w"` / `"m"` /
`"y"`）。结果会渲染成可读的标题/URL/snippet 块，而不是未包装的 JSON。

自定义后端只需要实现一个方法，也就是 `WebSearch` protocol：

```python
class WebSearch(Protocol):
    async def search(
        self, query: str, *, max_results: int = 5, time_range: str | None = None
    ) -> list[SearchResult]: ...
```

后端必须能安全处理并发调用。显式传入后端（`web_search(impl)`）意味着缺失的
可选依赖会在构造时失败，而不是运行到一半才失败。

## 时间

`lovia.tools.time`：三个小工具。

- **`now`**（工具）：当前系统时钟时间，ISO-8601 格式；可选 `tz` 接收 IANA
  名称（如 `"Asia/Shanghai"`）。默认使用服务器本地时区。（Windows 上 IANA 名称需要
  `pip install tzdata`。）
- **`sleep`**（工具）：最多 sleep 60 秒，适合简单的“等一下再检查”流程。
- **`current_date(tz=None)`**：**不是工具**，而是返回
  [指令片段](agents.md#指令)的工厂，将当天日期写入系统提示词：

  ```python
  agent = Agent(name="researcher", model="<model>", tools=[duckduckgo_search()])
  agent.instruction(current_date())
  ```

  日期写入提示词后，模型搜索时可以直接使用当前年份，无需先调用一轮 `now`。这里只写日期，
  不写精确时间，因为日期在提示词缓存周期内通常不会变化，对
  [Provider 缓存](providers.md#提示词缓存)影响很小。需要精确时间时再调用 `now`。

## 询问人工

`lovia.tools.human.ask_human(channel)`：让**模型**在运行中请求操作员输入
（和审批相反，审批是 **runner** 问“能不能做”）：

```python
from lovia import Agent
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()
agent = Agent(name="assistant", model="<model>", tools=[ask_human(channel)])

# 在操作员侧处理问题
async for q in channel.questions():   # channel.close() 后结束
    channel.answer(q.id, "使用选项 A。")
```

Tool 调用会阻塞，直到答案到达、问题被取消，或 Channel 关闭。

| API | 效果 |
| --- | --- |
| `questions()` | 异步迭代已经排队的问题；只允许一个消费者 |
| `pending` | 用于轮询的未回答问题快照 |
| `answer(id, text)` | 回答问题；Tool 返回 `text` |
| `cancel(id, reason=...)` | 用模型可见的 `ToolError` 取消一个问题 |
| `close(reason=...)` | 取消所有未回答问题、结束迭代，并拒绝后续提问 |

取消和关闭会成为 Tool 错误结果，因此模型可以在没有答案时继续。操作员可能离线时，应为
Tool 增加超时。从其他线程回答时，先切回事件循环线程，例如使用
`loop.call_soon_threadsafe(channel.answer, qid, text)`。

如果问题是“是否允许执行”，并且答案为允许或拒绝，应使用审批；如果模型需要向人询问信息，
并接收自由文本回答，则使用 `ask_human`。

## 注意事项

- **谨慎开放 `read_page` 和 `http_request`。** 模型受到不可信输入影响时，攻击者可能
  借此发起 SSRF 请求。对外提供服务前，应为工具启用审批，或隔离其网络环境。
- **`read_page` 默认缓存响应 5 分钟。** 变化比这更快的页面需要
  `page_reader(HttpReader(cache_ttl=0))`。
- **`duckduckgo_search()` / `tavily_search()` 会立即创建后端。** 缺少 `ddgs` 包或
  `TAVILY_API_KEY` 时，构建工具便会抛出 `UserError`，让配置问题在启动阶段暴露，
  而不是拖到运行中途。
- **搜索结果质量取决于后端。** DDG 后端不需要 API 密钥，但实际使用中可能遇到频率限制；
  生产环境通常使用 Tavily 等需要密钥的后端，或自行实现 `WebSearch`。

## 延伸阅读

- [工具](tools.md)：这些工具如何构建；你自己的工具也按同样方式写
- [工作区](workspace.md)：文件和 shell 工具
- 示例：[`tools/01_http.py`](../../examples/tools/01_http.py)，
  [`tools/02_time.py`](../../examples/tools/02_time.py)，
  [`tools/03_search.py`](../../examples/tools/03_search.py)，
  [`tools/04_human.py`](../../examples/tools/04_human.py)
