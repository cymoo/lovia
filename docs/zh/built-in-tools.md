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
    model="glm-5.2",
    tools=[http_fetch, duckduckgo_search(), now],
)
```

文件和 shell 工具来自[工作区](workspace.md)。`ask_human` 会在[人工介入](human-in-the-loop.md#询问人工)
中完整说明，下面只做简要介绍。

## HTTP 请求

`lovia.tools.http.http_fetch`：带边界限制、可识别内容类型（content type）的一次性请求工具。

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

`lovia.tools.search`：可插拔搜索工具。内置后端是 DuckDuckGo（不需要 API key），
位于 `ddg` extra 中：

```bash
pip install "lovia[ddg]"
```

```python
from lovia.tools.search import duckduckgo_search, web_search

tools = [duckduckgo_search()]            # 内置后端
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
  agent = Agent(name="researcher", model="glm-5.2", tools=[duckduckgo_search()])
  agent.instruction(current_date())
  ```

  日期在 prompt 里，模型搜索时就会带上当前年份，而不是先浪费一轮调用 `now`。它只写
  日期是有意为之：日期在任意 prompt cache 窗口内基本恒定，不会实质破坏
  [provider 缓存](providers.md#提示词缓存)。精确时间需要时交给 `now`。

## 询问人工

`lovia.tools.human.ask_human(channel)`：让**模型**在运行中请求操作员输入
（和审批相反，审批是 **runner** 问“能不能做”）：

```python
from lovia.tools.human import HumanChannel, ask_human

channel = HumanChannel()
agent = Agent(name="assistant", model="glm-5.2", tools=[ask_human(channel)])

# 在操作员侧处理问题
async for q in channel.questions():   # channel.close() 后结束
    channel.answer(q.id, "使用选项 A。")
```

工具调用会阻塞，直到答案到达、问题被取消，或 channel 关闭。完整语义，包括轮询、取消、
线程安全，见[人工介入](human-in-the-loop.md#询问人工)。

## 注意事项

- **`http_fetch` 是最锋利的内置工具。** 和不可信输入组合时，它就是 SSRF 原语。
  公开暴露前请加门禁或沙箱网络。
- **`duckduckgo_search()` 会立即构造。** 缺少 `ddgs` 包时，它会在构建时抛
  `UserError`。这正是你想在启动时看到的快速失败，而不是运行中捕获后忽略。
- **搜索结果质量取决于后端。** DDG 后端不需要 key，但实际使用中有频率限制；
  生产应用通常会换成自己的 `WebSearch`。

## 延伸阅读

- [工具](tools.md)：这些工具如何构建；你自己的工具也按同样方式写
- [工作区](workspace.md)：文件和 shell 工具
- 示例：[`tools/01_http.py`](../../examples/tools/01_http.py)，
  [`tools/02_time.py`](../../examples/tools/02_time.py)，
  [`tools/03_search.py`](../../examples/tools/03_search.py)，
  [`tools/04_human.py`](../../examples/tools/04_human.py)
