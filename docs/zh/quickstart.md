# 快速上手

从零开始，到写出一个支持流式输出、会调用工具的 agent，大约十分钟。本页所有
代码片段在模型配置好之后都可以直接运行。

## 安装

```bash
pip install lovia
```

Python 3.10+。核心安装刻意保持很小（`httpx`、`pydantic`、`pyyaml`）；
其他能力，如 Web UI、MCP、搜索，都是[可选 extra](#下一步)。

## 配置模型

lovia 不绑定默认供应商：你指定使用哪个模型，provider 自己从环境变量读取凭证。
最短路径是：

```bash
export OPENAI_API_KEY="sk-..."
```

这对应官方 OpenAI API 上的 `model="openai:gpt-5.5"`。常见变体有两个：

- **OpenAI 兼容服务**（DeepSeek、Ollama、vLLM 等）：再设置
  `OPENAI_BASE_URL`，模型名直接写裸名，比如 `model="deepseek-v4-pro"`，
  就会走 OpenAI 兼容 provider。
- **Anthropic**：设置 `ANTHROPIC_API_KEY`，使用
  `model="anthropic:claude-4-8-opus"`。

fallback 链、采样设置、自定义适配器等 provider 细节见
[Provider 与模型](providers.md)。

## Hello, agent

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="回答要具体、简洁。",
    model="openai:gpt-5.5",  # 也可以是 "deepseek-v4-pro"、"anthropic:claude-4-8-opus" 等
)

result = agent.run_sync("用一句话打个招呼。")
print(result.output)
```

`run_sync()` 适合脚本和 REPL，它会自己管理事件循环。在异步代码里请用
`await agent.run(...)`；如果在已经运行的事件循环里调用 `run_sync`，lovia 会抛出
`UserError`，并在 hint 里告诉你该怎么改。

`Agent` 只是配置，不保存对话状态，所以一个实例可以安全地被多个请求共享。为了
避免把模型字符串写死，`lovia.model_from_env()` 会从环境变量读取 `LOVIA_MODEL`；
如果没配置，会带着设置提示明确失败。示例都使用这种写法。

## 给它一个工具

任何带类型标注的 Python 函数都可以变成工具。模型看到的 schema 来自函数签名和
docstring，不需要维护一份单独定义。

```python
from lovia import Agent, tool


@tool
def check_inventory(sku: str) -> str:
    """查询某个商品 SKU 的库存。"""
    return f"{sku}: 库存 41 件"


agent = Agent(
    name="shop-assistant",
    instructions="帮助顾客解答商品问题。",
    model="openai:gpt-5.5",
    tools=[check_inventory],
)

result = agent.run_sync("SKU-1401 还有货吗？")
print(result.output)
```

工具既可以是同步函数（会在线程池里运行），也可以是 `async def`（直接 await）。
如果模型在同一轮里请求多个工具调用，默认会并发执行。如何关闭并发、设置重试、
超时和审批门禁，见[工具](tools.md)。

## 流式输出

做 UI 时，你通常不想等到最后才拿到 `RunResult`，而是希望事件一发生就收到：

```python
import asyncio

from lovia import Runner, events


async def main() -> None:
    handle = Runner.stream(agent, "SKU-1401 现在库存如何？")

    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.ToolCallStarted):
            print(f"\n[正在调用 {ev.call.name}...]")

    result = await handle.result()
    print(f"\n\n({result.usage.total_tokens} tokens)")


asyncio.run(main())
```

这个 handle 既是异步可迭代对象（产生类型化事件），也是 awaitable（得到最终
`RunResult`）。迭代不会因为运行失败而抛异常；每个流都会以且仅以一个终止事件
结束。`handle.result()` 返回结果，或抛出运行期间的错误。完整事件目录见
[流式输出](streaming.md)。

## 获取类型化输出

传入 Pydantic 模型（也可以是 dataclass、`TypedDict`、普通类型），最终答案就会
自动解析并校验：

```python
from pydantic import BaseModel

from lovia import Agent


class Availability(BaseModel):
    sku: str
    in_stock: bool
    units: int


agent = Agent(
    name="inventory",
    model="openai:gpt-5.5",
    tools=[check_inventory],
    output_type=Availability,
)

result = agent.run_sync("SKU-1401 还有货吗？")
print(result.output.units)  # 校验后的 int，不是需要你再解析的字符串
```

如果模型答案没有通过校验，lovia 默认会让它修复一次，然后才放弃。细节见
[结构化输出](structured-output.md)。

## 立即打开一个 playground

不写服务端代码，也可以直接和 agent 聊天：

```bash
pip install "lovia[web]"
python -m lovia.web
```

这会创建一个默认 agent：模型来自环境变量；如果 `./skills` 存在，就加载技能；
启用长期记忆、todo list，以及当前目录上的工作区；然后在
`http://127.0.0.1:8000` 启动聊天 UI。要换成你自己的 agent：
`python -m lovia.web --app mymodule:agent`。见
[Web UI 与服务端](web.md)。

## 下一步

- **[核心概念](concepts.md)**：花十分钟把后面的文档都变短。这里解释一次运行
  真正做了什么，以及状态到底放在哪里。
- **[示例](../../examples/README-zh.md)**：按编号排列的学习路径
  （`01_hello.py` 到 `30_support_bot.py`）。准备方式：

  ```bash
  pip install -e ".[examples,web]"   # 在仓库 checkout 中运行
  cp .env.example .env               # 设置 LOVIA_MODEL 和 API key
  python examples/01_hello.py
  ```

- **按需安装 extra**：

  | 需求 | 安装 |
  | --- | --- |
  | Web UI + HTTP API | `pip install "lovia[web]"` |
  | MCP 服务器 | `pip install "lovia[mcp]"` |
  | DuckDuckGo 搜索 | `pip install "lovia[ddg]"` |

- 之后按功能查阅[指南](README.md#指南)：多轮聊天用 session，敏感工具用审批，
  长对话用压缩，CI 质量检查用 eval。
