# 内置工具

内置工具不会自动挂载到 Agent 上。每项内置能力都必须显式导入，因此只需查看 Agent 的
构造代码，就能清楚知道它具备哪些能力。

```python
from lovia import Agent
from lovia.tools.http import http_fetch
from lovia.tools.search import duckduckgo_search
from lovia.tools.time import now

agent = Agent(
    name="researcher",
    model="<model>",
    tools=[http_fetch, duckduckgo_search(), now],
)
```

文件和 Shell Tool 来自[工作区](workspace.md)。操作员输入 Tool 见下方[询问人工](#询问人工)。

## HTTP 请求

`lovia.tools.http.http_fetch` 用于发送单次 HTTP 请求，并限制下载大小、超时和返回内容长度；
它还会根据内容类型处理响应。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `url` | 必填 | 只允许绝对 `http://` / `https://` URL |
| `method` | `"GET"` | 任意 HTTP 方法 |
| `headers` | `None` | 可选请求头 |
| `body` | `None` | 对 POST/PUT/PATCH 以 JSON 发送 |
| `timeout` | `30.0` | 秒，范围 1–120 |
| `max_chars` | `20_000` | 结果上限，100–200,000 |

响应会被处理成适合模型阅读的形式：JSON 会紧凑地重新序列化，HTML 会提取可见文本，
其他文本原样通过，二进制只返回元数据。下载有 1 MB 硬上限；结果会按 `max_chars`
剪裁，并带明确的截断说明。每个结果都以状态头开头，如
`HTTP 200 · text/html · 3,214 chars`。工具会跟随重定向；TLS 遵守
[`LOVIA_HTTP_*` 设置](providers.md#网络超时代理tls)。

> **没有 SSRF 过滤。** 这个工具会请求主机能访问到的任何地址，包括私有和内网地址；
> 重定向也可能跳到那里。当模型会接触不可信输入时，请给它加门禁
> （`dataclasses.replace(http_fetch, needs_approval=True)`），或隔离网络。

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

- **谨慎开放 `http_fetch`。** 模型受到不可信输入影响时，攻击者可能借此发起 SSRF 请求。
  对外提供服务前，应为工具启用审批，或隔离其网络环境。
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
