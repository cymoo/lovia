# 快速上手

从安装一路学习到 Tool、流式输出和类型化结果。本页每个 Python 代码块都是完整脚本：把
`<model>` 替换为当前端点配置的模型名，保存到文件后即可运行。

## 1. 安装并配置凭证

```bash
pip install lovia
export OPENAI_API_KEY="<api-key>"
```

不带前缀的模型名使用 OpenAI-compatible 适配器。Anthropic、本地模型、网关和自定义 Base URL
请从[安装](installation.md#配置模型)复制对应配置。

## 2. 运行第一个 Agent

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="回答要具体、简洁。",
    model="<model>",
)

result = agent.run_sync("讲一个只有 Python 开发者会懂的冷笑话。")
print(result.output)
```

`Agent` 是可复用配置，不保存对话状态。普通脚本使用 `run_sync()`；异步应用使用
`await agent.run(...)`，两者执行同一个 RunLoop。

## 3. 为 Agent 添加 Tool

`@tool` 把带类型信息的函数变成模型可调用的能力。函数签名会转换为 JSON Schema，文档字符串
则告诉模型何时调用它。

```python
from lovia import Agent, tool


@tool
def check_inventory(sku: str) -> str:
    """查询商品 SKU 的库存。"""
    return f"{sku}: 库存 41 件"


agent = Agent(
    name="shop-assistant",
    instructions="遇到库存事实问题时使用工具。",
    model="<model>",
    tools=[check_inventory],
)

result = agent.run_sync("SKU-1401 有货吗？再给一句购买建议。")
print(result.output)
print(f"turns={result.turns}, tokens={result.usage.total_tokens}")
```

如果模型调用 `check_inventory`，一个 Turn 请求并执行 Tool，下一 Turn 使用结果。详见
[核心概念](concepts.md#run-与-turn)。

## 4. 流式接收文本和 Tool 事件

UI 或 CLI 需要在最终答案前响应时，使用 `Runner.stream()`。`RunHandle` 既是异步事件流，
也是可等待的结果。

```python
import asyncio

from lovia import Agent, Runner, events, tool


@tool
def check_inventory(sku: str) -> str:
    """查询商品 SKU 的库存。"""
    return f"{sku}: 库存 41 件"


async def main() -> None:
    agent = Agent(name="shop-assistant", model="<model>", tools=[check_inventory])
    handle = Runner.stream(agent, "SKU-1401 的库存是多少？")

    async for event in handle:
        if isinstance(event, events.TextDelta):
            print(event.delta, end="", flush=True)
        elif isinstance(event, events.ToolCallStarted):
            print(f"\n[正在调用 {event.call.name}]", flush=True)

    result = await handle.result()
    print(f"\n\n{result.usage.total_tokens} tokens")


asyncio.run(main())
```

事件流以 `RunCompleted` 或 `RunFailed` 结束；调用 `result()` 会返回结果或抛出保存的异常。
详见[流式输出](streaming.md)。

## 5. 返回经过校验的数据

下游代码需要对象而不是自然语言时，把 Pydantic Model 作为 `output_type`。

```python
from pydantic import BaseModel

from lovia import Agent


class CityFact(BaseModel):
    city: str
    country: str
    population_millions: float


agent = Agent(
    name="researcher",
    instructions="返回当前近似数值。",
    model="<model>",
    output_type=CityFact,
)

result = agent.run_sync("给出一条上海的城市事实记录。")
print(result.output.city)
print(result.output.population_millions)
```

lovia 会校验最终答案并返回 `CityFact`。Provider 支持时使用原生 JSON Schema，否则使用可移植
的 Tool fallback。详见[结构化输出](structured-output.md)。

## 6. 打开聊天 UI

```bash
pip install "lovia[web]"
lovia web
```

打开 `http://127.0.0.1:8000`。首次运行向导可以收集并保存模型配置。要服务自己模块中的 Agent，
使用 `lovia web --app mymodule:agent`；详见 [Web UI](web-ui.md)。

## 选择下一步

| 目标 | 指南 | 示例 |
| --- | --- | --- |
| 持久化对话 | [Session 与 Checkpoint](sessions-and-checkpoints.md) | [`05_sessions.py`](../../examples/05_sessions.py) |
| 添加文件与 Shell 能力 | [工作区](workspace.md) | [`20_workspace_agent.py`](../../examples/20_workspace_agent.py) |
| 为副作用加入审批 | [工具审批](tools.md#工具审批) | [`12_approval.py`](../../examples/12_approval.py) |
| 添加 Skills、Todo 或 Memory | [插件](plugins.md) | [示例](../../examples/README-zh.md) |
| 加入重试和成本限制 | [Provider 重试](retries.md) · [预算](budgets.md) | [`14_reliability.py`](../../examples/14_reliability.py) |
| 不访问网络进行测试 | [测试](testing.md) | [`22_testing.py`](../../examples/22_testing.py) |
