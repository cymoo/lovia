# 内置工具

内置工具都要显式导入和挂载。查看 Agent 的构造代码，就能知道它可以访问哪些外部能力。

```python
from lovia import Agent
from lovia.tools import duckduckgo_search, http_request, now, read_page

agent = Agent(
    name="researcher",
    model="<model>",
    tools=[read_page, http_request, duckduckgo_search(), now],
)
```

文件和 Shell 工具由[工作区](workspace.md)提供；需要人工补充信息时，使用
[询问人工](#询问人工)。

| 需求 | 工具 |
| --- | --- |
| 把网页正文转成 Markdown | `read_page` |
| 调用 JSON / HTTP API | `http_request` |
| 搜索公开网页 | `duckduckgo_search()` / `tavily_search()` |
| 获取当前时间 | `now` |
| 暂停并询问用户 | `ask_human` |

## 读取网页

`lovia.tools.read_page` 将 HTML 正文转换为保留结构和链接的 Markdown。

`read_page` 向模型公开三个参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `url` | 必填 | 只允许绝对 `http://` / `https://` URL |
| `images` | `False` | 同时返回去重后的图片清单 |
| `offset` | `0` | 从这个字符偏移处继续读一个长页面 |

超时、下载上限和缓存时间等配置由调用方决定，因此放在 `page_reader()` 工厂上，
不会出现在模型每次调用所见的 JSON Schema 中：

```python
from lovia.tools import HttpReader, page_reader

tool = page_reader(                     # 或直接用现成的 read_page
    HttpReader(timeout=15.0, max_bytes=2_000_000, cache_ttl=60),
    max_chars=40_000,
)
```

### 返回结果

```
Title: Example Domain
URL: https://example.com/guide
HTTP 200 · text/html

# Example Domain
[... truncated. Continue with offset=20000.]

Images (1):
1. https://example.com/img/hero.png — Hero banner
```

页面过长时，截断提示会给出下一次应传入的 `offset`，便于分段续读。`read_page`
返回的 `Page` 包含最终 URL、状态码、标题、Markdown 正文和图片列表；上例是默认渲染结果。

### 图片

Markdown 正文已经包含可用的 `<img src>`。设置 `images=True` 后，结果还会附上一份
去重并转换为绝对 URL 的图片清单，其中也包括 Markdown 无法完整表达的来源：
仅提供 `srcset` 时尺寸最大的候选、`<picture><source>` 和 `og:image`。相对 URL 会以
`<base href>` 和重定向后的 URL 为基准解析；`data:`、`javascript:` 和页内锚点会被忽略，
以免大段内联 base64 数据占用结果空间。

### 需要 JavaScript 时更换后端

`HttpReader` 通过普通 HTTP 请求获取页面，并使用标准库解析 HTML，不执行客户端
JavaScript。因此，纯前端渲染的单页应用可能只返回一个空壳。需要浏览器渲染或托管
抽取服务时，实现 `PageReader` 协议即可；它的接口形式与 `WebSearch` 一致：

```python
class PageReader(Protocol):
    async def read(self, url: str, *, images: bool = False) -> Page: ...
```

下面以直接返回 Markdown 的 Jina Reader 为例：

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

字符数限制由工具统一处理，因此后端应返回完整正文。

### 缓存与上限

响应按 URL 缓存 `cache_ttl` 秒（默认 300，最多保留 `cache_size` 条）。缓存不只是为了
加速：如果续读时重新下载，`offset` 前后可能来自网页的两个不同版本。单次下载上限为
1 MB；达到上限时，`size_capped` 会设为 `True`，结果中也会说明尾部尚未下载，继续增大
`offset` 无法读取这部分内容。`4xx`/`5xx` 响应的正文最多保留 500 个字符，避免错误页
占满结果空间。

## HTTP 请求

`lovia.tools.http_request` 适合调用 REST API 和其他非 HTML 端点。它会原样返回 HTML，
不会像 `read_page` 那样提取网页正文。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `url` | 必填 | 只允许绝对 `http://` / `https://` URL |
| `method` | `"GET"` | 任意 HTTP 方法 |
| `headers` | `None` | 可选请求头（要覆盖默认 UA 也在这里） |
| `body` | `None` | 作为 JSON 请求体发送，通常用于 POST/PUT/PATCH |
| `timeout` | `30.0` | 秒，范围 1–120 |
| `max_chars` | `20_000` | 结果上限，100–200,000 |

结果由状态行、响应头和正文组成。JSON 会压缩后返回，文本保持原样，二进制响应只返回
元数据。响应头可用于读取限流信息和 `Link:` 分页；`set-cookie` 会被隐藏，以免会话令牌
写入运行记录（transcript）。单次下载上限为 1 MB，正文超过 `max_chars` 时会截断并给出提示。
工具会跟随重定向；如果最终 URL 与请求 URL 不同，结果中会明确列出。TLS 设置见
[`LOVIA_HTTP_*` 配置](providers.md#网络超时代理tls)。

> **不会过滤 SSRF 风险。** `read_page` 和 `http_request` 可以访问当前主机能够访问的
> 任何地址，包括内网地址；重定向也可能跳转到内网。如果 Agent 可能处理不可信内容，
> 请为工具启用审批或隔离网络。

如果只想审批可能修改服务端状态的请求，可以使用内置谓词。该谓词不会默认启用，因为
审批采用“无人处理即拒绝”的策略：使用 `Runner.run()` 且未配置审批处理器时，POST 等
请求都会被拒绝。

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
`"y"`）。结果会整理为标题、URL 和摘要，而不是直接返回 JSON。

自定义后端只需实现 `WebSearch` 协议中的 `search()` 方法：

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
- **`sleep`**（工具）：最多等待 60 秒，适合简单的“等一下再检查”流程。
- **`current_date(tz=None)`**：**不是工具**，而是返回
  [指令片段](agents.md#编写指令)的工厂，将当天日期写入系统提示词：

  ```python
  agent = Agent(name="researcher", model="<model>", tools=[duckduckgo_search()])
  agent.instruction(current_date())
  ```

  日期写入提示词后，模型搜索时可以直接使用当前年份，无需先调用一轮 `now`。这里只写日期，
  不写精确时间，因为日期在提示词缓存周期内通常不会变化，对
  [Provider 缓存](providers.md#提示词缓存)影响很小。需要精确时间时再调用 `now`。

## 询问人工

`lovia.tools.human.ask_human(channel)` 让**模型**在运行中请求操作员输入。它与审批的
方向相反：审批由 **Runner** 询问“是否允许执行”。

```python
from lovia import Agent
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()
agent = Agent(name="assistant", model="<model>", tools=[ask_human(channel)])

# 在操作员侧处理问题
async for q in channel.questions():   # channel.close() 后结束
    channel.answer(q.id, "使用选项 A。")
```

工具调用会等待，直到收到答案、问题被取消或 Channel 关闭。

当问题是单选（或配合 `multi_select` 的多选）时，模型会附带 2–4 个 `options`——每个
`QuestionOption` 有 `label` 和可选的一行 `description`。这些选项只是建议，任何字符串仍是
合法答案。多选答案用换行连接所选 label；schema 强制 label 单行且互不重复，因此结果没有歧义：

```python
async for q in channel.questions():
    if q.options:
        print(q.question, [o.label for o in q.options], q.multi_select)
    channel.answer(q.id, "Kyoto\nOsaka")
```

每个问题还携带发问运行的 `session_id` / `run_id`（若有），服务多个会话的消费者可以据此
路由。`ask_human` 是执行屏障（`parallel=False`）：一个运行同一时刻至多一个挂起问题。

| API | 效果 |
| --- | --- |
| `questions()` | 异步迭代已经排队的问题；只允许一个消费者 |
| `pending` | 用于轮询的未回答问题快照 |
| `answer(id, text)` | 回答问题；工具返回 `text` |
| `cancel(id, reason=...)` | 用模型可见的 `ToolError` 取消一个问题 |
| `close(reason=...)` | 取消所有未回答问题、结束迭代，并拒绝后续提问 |

取消和关闭会成为工具错误结果，因此模型可以在没有答案时继续。操作员可能离线时，应为
工具设置超时。从其他线程回答时，先切回事件循环线程，例如使用
`loop.call_soon_threadsafe(channel.answer, qid, text)`。

如果问题是“是否允许执行”，并且答案为允许或拒绝，应使用审批；如果模型需要向人询问信息，
并接收自由文本回答，则使用 `ask_human`。

## 使用建议

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
