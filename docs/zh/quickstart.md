# 快速上手

三步跑通：安装 lovia，配置模型，写一个会调用工具的 agent。本页代码片段在模型配置好后可以直接运行。

## 安装

```bash
pip install lovia
```

Python 3.10+。核心依赖只有 `httpx`、`pydantic`、`pyyaml`；Web UI、MCP、搜索等能力通过 extra 按需安装。

## 配置模型

OpenAI 官方 API 以及 DeepSeek、Ollama、vLLM 等 OpenAI 兼容端点：配置
`OPENAI_BASE_URL` 和 `OPENAI_API_KEY`，模型名直接写服务提供的裸名，例如
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

## 第一个 agent

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

脚本里用 `run_sync()`；异步代码里用 `await agent.run(...)`。

## 加一个工具

普通 Python 函数加上 `@tool`，就能被模型调用。schema 来自类型标注和 docstring。

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

更多工具选项，如并发、重试、超时和审批，见[工具](tools.md)。

## 流式输出

做 UI 时，可以边生成边渲染：

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

需要对象而不是字符串时，传 `output_type`：

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
python -m lovia.web
```

默认会在 `http://127.0.0.1:8000` 启动聊天 UI。要换成你自己的 agent：
`python -m lovia.web --app mymodule:agent`。

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
