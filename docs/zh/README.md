# lovia

lovia 是一个轻量的 Python Agent 框架。先用一个 `Agent` 和几行 Python 跑起来；需要时再加入
Tool、流式事件、持久化、Workspace 或 Web UI。

```bash
pip install lovia
```

先在[快速开始：配置模型](quickstart.md#2-配置模型)中设置端点，再把下面的 `<model>`
换成端点提供的模型名。

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="用清楚、具体的语言回答。",
    model="<model>",
)

print(agent.run_sync("天空为什么是蓝色的？").output)
```

[完成首次运行 →](quickstart.md){ .md-button .md-button--primary }
[打开 Web UI →](web-ui.md){ .md-button }

## 从你的目标开始

| 我想要… | 先看 | 再深入 |
| --- | --- | --- |
| 跑通第一个 Agent | [快速开始](quickstart.md) | [核心概念](concepts.md) |
| 接入模型或网关 | [配置模型](quickstart.md#2-配置模型) | [Provider 与模型](providers.md) |
| 让模型调用代码 | [工具](tools.md) | [内置工具](built-in-tools.md) |
| 读取文件、执行命令 | [工作区](workspace.md) | [工具审批](tools.md#工具审批) |
| 保存对话或恢复任务 | [Session 与 Checkpoint](sessions-and-checkpoints.md) | [上下文管理](context.md) |
| 组合多个 Agent | [多 Agent](multi-agent.md) | [插件](plugins.md) |
| 做成聊天应用 | [Web UI](web-ui.md) | [Web 服务端](web-server.md) · [HTTP API](http-api.md) |
| 测试 Agent 行为 | [测试](testing.md) | [评测](eval.md) · [可观测性](observability.md) |

## lovia 的取舍

- 核心只依赖 `httpx`、`pydantic` 和 `pyyaml`，其他集成按需安装。
- `Agent` 只保存配置；每次 Run 的可变状态由运行循环管理。
- 上下文压缩只改变模型 View，不改写本次 Run 的 Transcript；超大 Tool 输出仍受配置上限约束。
- Skills、MCP、Todo、Memory 和自定义能力共用 Plugin 扩展点。

## 用示例学习

示例按难度排列，每个文件都可以单独运行：

- [`01_hello.py`](../../examples/01_hello.py)：第一次运行
- [`02_tools.py`](../../examples/02_tools.py)：Tool 调用
- [`03_streaming.py`](../../examples/03_streaming.py)：流式事件
- [`04_structured_output.py`](../../examples/04_structured_output.py)：结构化输出
- [`05_sessions.py`](../../examples/05_sessions.py)：对话历史
- [全部示例](../../examples/README-zh.md)

!!! note "版本说明"

    本站对应 `main` 分支，可能比已安装的版本更新。运行
    `python -c "import lovia; print(lovia.__version__)"` 可以查看本地版本。

修改框架本身时，请先阅读[架构说明](../architecture.md)。

English documentation: [docs/en](../en/README.md).
