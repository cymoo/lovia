# lovia

**轻量、优雅、Provider 中立的 Python Agent 框架。** 从一个 Agent 和类型化 Tool 开始；只有应用
真正需要时，再加入流式输出、持久化、上下文管理、插件、Workspace 或 Web UI。

```bash
pip install lovia
```

```python
from lovia import Agent

agent = Agent(
    name="assistant",
    instructions="先给结论；涉及操作时，补充一个可执行的下一步。",
    model="<model>",
)

result = agent.run_sync("用三句话解释天空为什么是蓝色的。")
print(result.output)
```

[创建第一个 Agent →](quickstart.md){ .md-button .md-button--primary }
[打开 Web UI →](web-ui.md){ .md-button }

## 为什么选择 lovia

<div class="grid cards" markdown>

-   **轻量**

    Core 只需要 HTTP 请求库和数据验证库。

-   **Provider 中立**

    可接入 OpenAI、Anthropic、兼容端点或自定义 Provider。

-   **类型化且可观察**

    函数类型注解自动生成 Tool Schema。Run 提供类型化事件、权威 Transcript、用量和结构化错误。

-   **渐进式扩展**

    从单文件脚本开始；以后可加入 Plugin、Session、Checkpoint、压缩、审批和 Workspace，
    不必替换核心编程模型。

</div>

## 核心心智模型

```text
Agent 配置 + 输入
        │
        ▼
Runner 创建一个 RunLoop
        │
        ▼
权威 Transcript ─► 上下文策略 ─► Provider 视图 ─► 模型 Turn
      ▲                                              │
      └── Tool 结果 ◄─ 审批 / 超时 / 预算 ◄─ Tool 调用
```

`Agent` 是不可变配置；RunLoop 是一次 Run 中唯一持有可变状态的引擎。模型和 Tool 的
结果都追加到权威 Transcript，上下文管理只从中派生更小的 Provider 视图，不改写记录。
类型化事件实时暴露同一个循环；Session 追加已完成的 Run，Checkpoint 保存可恢复的运行中
状态。正因为这些职责分开，流式输出、持久化和上下文压缩才不会变成几套互相冲突的状态。
完整生命周期见[核心概念](concepts.md)。

## 按目标选择路径

| 我想要… | 从这里开始 | 接着加入 |
| --- | --- | --- |
| 创建第一个 Agent | [快速开始](quickstart.md) | [Agent](agents.md)、[运行 Agent](running.md) |
| 连接模型或网关 | [快速开始](quickstart.md#2-配置模型) | [Provider 与模型](providers.md) |
| 为模型提供能力 | [工具](tools.md) | [内置工具](built-in-tools.md)、[工作区](workspace.md) |
| 构建长期运行的助手 | [插件](plugins.md) | [Skills](skills.md)、[Todo](todo.md)、[记忆](memory.md) |
| 让 Run 适合生产环境 | [Provider 重试](retries.md) | [预算](budgets.md)、[Session](sessions-and-checkpoints.md)、[护栏](guardrails.md) |
| 添加聊天体验 | [Web UI](web-ui.md) | [Web 服务端](web-server.md)、[HTTP API](http-api.md) |
| 测试行为 | [测试](testing.md) | [评测](eval.md)、[可观测性](observability.md) |

## 从可运行示例学习

仓库中的示例按功能组成学习路径，每个脚本都足够小，可以直接复制修改：

- [`01_hello.py`](../../examples/01_hello.py)：一个 Agent，一次回答
- [`02_tools.py`](../../examples/02_tools.py)：类型化 Tool 调用
- [`03_streaming.py`](../../examples/03_streaming.py)：类型化事件
- [`04_structured_output.py`](../../examples/04_structured_output.py)：经过校验的输出
- [`05_sessions.py`](../../examples/05_sessions.py)：对话历史
- [浏览全部示例](../../examples/README-zh.md)

!!! note "文档版本"

    本站跟随当前 `main` 分支。可运行
    `python -c "import lovia; print(lovia.__version__)"` 查看已安装版本并进行对照。

## 面向贡献者

[架构说明](../architecture.md)记录了模块地图、RunLoop、Transcript 约束、Plugin、持久化与
上下文压缩机制。

---

English documentation: [docs/en](../en/README.md).
