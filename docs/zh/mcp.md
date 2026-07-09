# MCP

[Model Context Protocol](https://modelcontextprotocol.io) 服务器可以暴露 agent 可调用的
工具，比如文件系统、浏览器、数据库，而不需要你手写适配器。lovia 的 `MCP` 插件会连接服务器，
把这些工具转换成普通 [`Tool`](tools.md)，并按运行边界管理连接生命周期。

```bash
pip install "lovia[mcp]"
```

```python
from lovia import Agent
from lovia.plugins.mcp import MCP, MCPServerStdio

agent = Agent(
    name="assistant",
    model="glm-5.2",
    plugins=[
        MCP(MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"]))
    ],
)
```

只有真正打开连接时才会 import `mcp` 依赖，所以 `lovia.plugins.mcp` 始终可以安全导入；
缺包会在打开连接时抛带安装提示的 `UserError`。

## 服务器

两种传输，都是 frozen、keyword-only 配置：

```python
MCPServerStdio(command="uvx", args=["mcp-server-fetch"], env=None, name="web")
MCPServerStreamableHTTP(url="https://mcp.example.com/mcp", headers=None, name="api")
```

两种配置共享的选项：

| 选项 | 默认值 | 作用 |
| --- | --- | --- |
| `name` | `None` | 给服务器工具加前缀：`name="web"` → `web__fetch` |
| `include_tools` / `exclude_tools` | `None` | 原始工具名 allowlist / denylist |
| `needs_approval` | `False` | bool 或谓词；让这个服务器的每个工具都走标准[审批流程](human-in-the-loop.md) |
| `retries` / `timeout` / `max_output_chars` / `result_renderer` | `None` | 应用于每个转换工具的工具级策略（见[工具](tools.md)） |
| `auto_reconnect` | `True` | 连接失效后自动重开，并重试调用一次 |
| `close_after_run` | `True` | 运行结束时关闭连接 |

`MCP(a, b, ...)` 接受任意数量的服务器；前缀能避免工具名冲突（冲突会像其他重复工具名一样
在运行开始时报错）。

## 连接生命周期

**按运行打开（默认）。** 传入服务器**配置**时，每次运行都会在插件 `setup()` 中打开连接，并在
运行结束时关闭。这种方式无状态、稳健，代价是每次运行都要付出子进程/握手成本。

**持久连接。** 如果很多运行都会访问同一服务器，可以自己打开 session，再把已打开的连接
传进去。`MCPServerLike` 同时支持配置和连接：

```python
server = MCPServerStdio(name="web", command="uvx", args=["mcp-server-fetch"])

async with server.session() as conn:      # 只打开一次
    agent = Agent(name="assistant", model="glm-5.2", plugins=[MCP(conn)])
    await Runner.run(agent, "抓取 https://example.com 并总结。")
    await Runner.run(agent, "现在抓取 RFC 索引。")   # 同一连接
```

运行不会关闭你自己打开的连接（已打开的 `MCPConnection` 的 `close_after_run` 为 `False`）；
它的生命周期就是 `async with` block。一个持久连接适合**顺序**运行；单个 MCP session 不支持
并发运行。并发 worker 请各自拿自己的连接。

## MCP 工具如何表现

- 工具 schema 会规范化成普通 lovia `Tool`（`normalize_schema` 会修补松散 schema）；
  它们会像原生工具一样校验、渲染、截断，并出现在[流式事件](streaming.md)中。服务器上的
  自定义 `result_renderer` 接收原始 `MCPToolResult`；默认渲染是 `render_mcp_content`。
- **失败分两类。** 协议层工具失败（服务器返回 `isError`）会以 `[tool error] ...` 渲染给模型，
  让它自我修正，不会抛出。传输/连接失败会抛 `MCPError`（携带 `tool_name`），像普通工具
  异常一样结束该调用。
- 工具**结果**可以携带资源：文本内联；图片/音频变成带大小的 placeholder（不会放原始
  base64）；资源链接变成 `[resource link: uri]` 行。
- **范围有意只限工具。** MCP prompts、资源浏览、sampling、OAuth 和 subscriptions 都不是目标；
  这个插件只做一件事。

## 容易踩的点

- **`auto_reconnect` 意味着至少执行一次。** 调用中途断开后，会在新连接上重试一次；
  非幂等副作用（如 `create_ticket`）可能发生两次。对会修改状态的服务器，设置
  `auto_reconnect=False`，让模型看到错误。
- **MCP 工具默认并发运行**，和所有工具一样。如果服务器工具会修改共享状态，它们没有天然屏障；
  可以用 `include_tools` 拆成两个服务器条目，或用 `needs_approval` 给危险工具加门禁。
- **`needs_approval` 是按服务器，不是按工具。** “读工具自由，写工具门禁”的惯用做法，是把同一
  服务器拆成两个 `MCPServer` 条目（同 command，不同 `include_tools`）。
- **stdio 服务器默认继承你的进程环境**，除非传 `env=`；没有 `cwd` 选项。需要工作目录时，
  用包装脚本启动。

## 延伸阅读

- [插件](plugins.md)：底层机制
- [工具](tools.md)：转换后的 MCP 工具继承的一切行为
- [人工介入](human-in-the-loop.md)：给服务器工具加门禁
- 示例：[`24_mcp.py`](../../examples/24_mcp.py)
