# 快速上手

只需三步：安装 lovia、配置模型，再编写一个能够调用工具的 Agent。模型配置完成后，
本页所有代码片段都可以直接运行。

## 安装

```bash
pip install lovia
```

lovia 需要 Python 3.10 或更高版本。核心依赖只有 `httpx`、`pydantic` 和 `pyyaml`；
Web UI、MCP、搜索等功能均以可选依赖的形式按需安装。

## 配置模型

使用 OpenAI 官方 API，或 DeepSeek、Ollama、vLLM 等 OpenAI 兼容端点时，配置
`OPENAI_BASE_URL` 和 `OPENAI_API_KEY`，模型名直接写服务提供的不带前缀的名称，例如
`model="glm-5.2"`。

```bash
export OPENAI_BASE_URL="https://..."
export OPENAI_API_KEY="sk-..."
```

Anthropic 配置 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_API_KEY`，模型名使用
`anthropic:` 前缀，例如 `model="anthropic:<model>"`。

```bash
export ANTHROPIC_BASE_URL="https://..."
export ANTHROPIC_API_KEY="sk-ant-..."
```

更多模型写法见 [Provider 与模型](providers.md)。

## 创建第一个 Agent

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="回答要具体、简洁。",
    model="glm-5.2",
)

result = agent.run_sync("讲一个只有 Python 开发者会懂的冷笑话。")
print(result.output)
```

在普通脚本中使用 `run_sync()`；在异步代码中使用 `await agent.run(...)`。

## 加一个工具

用 `@tool` 装饰普通 Python 函数，模型便可以调用它。参数结构会根据类型标注和文档字符串自动推导。

```python
from lovia import Agent, tool


@tool
def check_inventory(sku: str) -> str:
    """查询某个商品 SKU 的库存。"""
    return f"{sku}: 库存 41 件"


agent = Agent(
    name="shop-assistant",
    instructions="帮助顾客解答商品问题。",
    model="glm-5.2",
    tools=[check_inventory],
)

result = agent.run_sync("SKU-1401 还有货吗？顺便给一句购买建议。")
print(result.output)
```

并发、重试、超时、审批等更多工具选项见[工具](tools.md)。

## 流式输出

做 UI 时，可以一边生成一边渲染：

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

完整事件清单见[流式输出](streaming.md)。

## 获取类型化输出

如果需要对象而不是字符串，传入 `output_type`：

```python
from pydantic import BaseModel

from lovia import Agent


class Availability(BaseModel):
    sku: str
    in_stock: bool
    units: int


agent = Agent(
    name="inventory",
    model="glm-5.2",
    tools=[check_inventory],
    output_type=Availability,
)

result = agent.run_sync("SKU-1401 还有货吗？")
print(result.output.units)
```

细节见[结构化输出](structured-output.md)。

## 立即打开一个聊天界面

```bash
pip install "lovia[web]"
lovia web
```

首次运行时，缺失的必要配置（模型；官方端点的 API key）会在终端交互式询问，并可保存到
`~/.config/lovia/config.env`。UI 默认在 `http://127.0.0.1:8000` 启动。
要换成你自己的 agent：`lovia web --app mymodule:agent`。

## 下一步

| 目标 | 阅读 |
| --- | --- |
| 看更多可运行脚本 | [示例](../../examples/README-zh.md) |
| 理解一次运行里发生了什么 | [核心概念](concepts.md) |
| 保存多轮对话 | [Session 与 Checkpoint](sessions-and-checkpoints.md) |
| 访问文件和 shell | [工作区](workspace.md) |
| 做 Web UI 或 HTTP API | [Web UI 与服务端](web.md)、[HTTP API](http-api.md) |
| 加审批、预算、重试 | [人工介入](human-in-the-loop.md)、[可靠性](reliability.md) |
| 做离线测试和行为评测 | [测试](testing.md)、[评测](eval.md) |
