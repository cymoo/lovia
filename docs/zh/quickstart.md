# 快速上手

创建一个带类型化工具的 Agent，运行它并查看结果。本页每个 Python 代码块都是自包含的，
可以直接复制到新文件中运行。

## 1. 安装并配置模型

```bash
pip install lovia
```

设置 `LOVIA_MODEL`，并提供当前端点所需的凭证和 Base URL。[安装与模型配置](installation.md#配置模型)
给出了 OpenAI、Anthropic、OpenAI-compatible、Anthropic-compatible 和 Ollama 的可编辑配置。

## 2. 运行 Agent

```python
from lovia import Agent, Runner, model_from_env

agent = Agent(
    name="assistant",
    instructions="回答要具体、简洁。",
    model=model_from_env(),
)

result = Runner.run_sync(
    agent,
    "讲一个只有 Python 开发者会懂的冷笑话。",
)
print(result.output)
```

普通脚本使用 `Runner.run_sync()`；异步代码使用 `await Runner.run(...)`。两者执行的是
同一套运行循环。

## 3. 添加 Tool

`@tool` 可以把带类型信息的普通函数变成模型可调用的能力。函数签名会转换为 JSON Schema，
文档字符串则用于告诉模型何时调用它。

```python
from lovia import Agent, Runner, model_from_env, tool


@tool
def check_inventory(sku: str) -> str:
    """查询某个商品 SKU 的库存。"""
    return f"{sku}: 库存 41 件"


agent = Agent(
    name="shop-assistant",
    instructions="遇到库存事实问题时使用工具。",
    model=model_from_env(),
    tools=[check_inventory],
)

result = Runner.run_sync(
    agent,
    "SKU-1401 还有货吗？顺便给一句购买建议。",
)
print(result.output)
print(f"turns={result.turns}, tokens={result.usage.total_tokens}")
```

如果模型调用 `check_inventory`，第一个 Turn 包含模型回复和工具执行；第二个 Turn 再让模型
根据工具结果作答。两者的区别详见[核心概念](concepts.md#run-与-turn)。

## 选择下一步

| 目标 | 指南 |
| --- | --- |
| 流式接收文本和工具事件 | [流式输出](streaming.md) · [`03_streaming.py`](../../examples/03_streaming.py) |
| 返回经过校验的对象 | [结构化输出](structured-output.md) · [`04_structured_output.py`](../../examples/04_structured_output.py) |
| 持久化多轮对话 | [Session 与 Checkpoint](sessions-and-checkpoints.md) · [`05_sessions.py`](../../examples/05_sessions.py) |
| 为 Agent 提供文件和 Shell | [工作区](workspace.md) · [`20_workspace_agent.py`](../../examples/20_workspace_agent.py) |
| 打开聊天 UI | [Web UI 与服务端](web.md) · [`26_web_serve.py`](../../examples/26_web_serve.py) |
| 浏览完整学习路径 | [可运行示例](../../examples/README-zh.md) |
