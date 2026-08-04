# 插件

一项可复用能力可能同时包含工具、提示词、每轮提醒、钩子和清理操作。插件（plugin）把它们
打包在一起；Skills、MCP、Todo 和 Memory 都使用这套扩展机制。

```python
from lovia import Agent, Memory, Skills, Todo

agent = Agent(
    name="builder",
    model="<model>",
    plugins=[Todo(), Skills("./skills"), Memory("./.lovia/memory")],
)
```

如果能力只有一个独立函数，直接使用 `@tool` 通常更清楚。只有当它需要同时携带
提示词、状态、每轮提醒或清理逻辑时，才值得写成 Plugin。

## 契约

任何拥有 `name` 和异步 `setup()` 方法、且能返回 `PluginInstance` 的对象，都可以作为插件：

```python
class Plugin(Protocol):
    name: str
    async def setup(self) -> PluginInstance: ...
```

Runner 会在**每次运行**调用并等待一次 `setup()`；如果 [Handoff](multi-agent.md) 到另一个
Agent，也会依次执行目标 Agent 插件的 `setup()`。返回内容会合并到运行循环的固定扩展点：

| `PluginInstance` 字段 | 效果 |
| --- | --- |
| `tools` | 合并进 Agent 工具集（共用一个命名空间；名称冲突时会报错） |
| `instructions` | 追加到 system prompt，运行开始时渲染一次 |
| `view_injectors` | **每轮**调用；返回的条目只进入本轮模型 View，不持久化 |
| `hooks` | 接收运行事件，与 Agent 自身的 `AgentHooks` 一起触发 |
| `input_guardrails` / `output_guardrails` | 与 Agent 自身的护栏合并，在既定检查点运行 |
| `aclose` | 运行结束时等待的 coroutine（多个插件按 LIFO 顺序尽力清理） |

插件的贡献内容只会合并到固定扩展点，Plugin 本身不接管控制流。中止、重试和 Handoff
仍由运行循环执行；插件护栏也只能在与 Agent 护栏相同的检查点终止运行。

`name` 是插件身份：每个 agent 内唯一（在任何 `setup()` 运行前校验），并且应保持稳定。

## 视图注入器：为每轮添加临时内容

这是一个比较特殊的扩展点。`ViewInjector` 每轮都会接收当前 `RunContext`，并返回要追加到**本轮模型
View** 的 `TranscriptEntry`：

```python
def inject(ctx: RunContext) -> list[TranscriptEntry] | None:
    if not store.items:
        return None
    return [InputEntry(role="user", content=f"<system-reminder>\n{render(store.items)}\n</system-reminder>")]
```

因为注入内容不进入 Transcript 或 Session，所以不会随轮次累积，也不会在恢复运行时回放。
它们还会保留 Provider 可缓存的 prompt 前缀。Injector 应在每轮重新生成提醒、时钟、Todo 列表等内容。
Injector **失败时放行**：异常会被记录并跳过，不会中止运行。实现应尽量小而快；注入发生在
[上下文策略](context.md)塑造模型 View 之后。

## 状态作用域

写插件时最需要想清楚的是状态放在哪里；这会直接决定并发下的行为：

- **每次运行的状态**在 `setup()` **内部**构建并被闭包捕获。每次运行都有新副本，天然并发安全。
  下面的 todo list 就是这样。
- **跨运行状态**（数据库、索引、术语表）放在插件对象上，在构造时传入。它被所有运行共享，
  可能同时访问，所以必须并发安全。插件也不会关闭它：生命周期属于创建它的人。
  [Memory](memory.md) 就是这样。

## 示例：跨会话术语表

一个有状态插件的完整形态：共享后端、一个工具、提示词文本，再加一小段代码：

```python
from dataclasses import dataclass
from typing import Protocol

from lovia import Agent, PluginInstance, tool


class Glossary(Protocol):
    """你的共享后端：DB、文件或内存 dict。"""

    async def define(self, term: str, meaning: str) -> None: ...
    async def lookup(self, term: str) -> str | None: ...


@dataclass
class GlossaryPlugin:
    """agent 可读写的跨会话术语表。"""

    store: Glossary          # 长生命周期，所有运行共享
    name: str = "glossary"

    async def setup(self) -> PluginInstance:
        store = self.store

        @tool
        async def define(term: str, meaning: str) -> str:
            """记录一个领域术语的含义，供当前和之后的会话使用。"""
            await store.define(term, meaning)
            return f"已记录：{term}。"

        @tool
        async def lookup(term: str) -> str:
            """查询之前定义过的领域术语。"""
            return await store.lookup(term) or f"没有 {term!r} 的定义。"

        return PluginInstance(
            tools=[define, lookup],
            instructions="当用户解释领域术语时，用 `define` 记录；"
            "在请用户重新解释前，先用 `lookup` 查询。",
        )


agent = Agent(name="assistant", model="<model>", plugins=[GlossaryPlugin(MyGlossary())])
```

如果插件在 `setup()` 中打开资源（MCP 连接、HTTP client），就通过 `aclose` 返回清理动作：

```python
async def setup(self) -> PluginInstance:
    conn = await open_connection(self.url)
    return PluginInstance(tools=tools_from(conn), aclose=conn.close)
```

## 内置插件

| 插件 | 一句话 | 指南 |
| --- | --- | --- |
| `Todo()` | 外置 Checklist，每轮重新展示 | [Todo](todo.md) |
| `Skills(...)` | 带渐进披露的指令包 | [Skills](skills.md) |
| `MCP(...)` | Model Context Protocol 服务器提供的工具 | [MCP](mcp.md) |
| `Memory(...)` | 跨会话长期记忆 | [记忆](memory.md) |

## 设计建议

- **把运行状态放在插件对象上是并发 bug。** 同一个 agent 的两个并发运行共享插件实例；
  不在 `setup()` 内部创建的可变内容，都是共享可变状态。
- **`setup()` 按 agent、按运行执行，包括 handoff 目标。** 同一个插件如果挂在 handoff
  两侧，会在一次运行中激活两次；请让 `setup()` 保持轻量，并且效果上幂等。
- **instructions 在一次运行内是静态的。** `PluginInstance.instructions` 只在运行开始时渲染一次；
  需要每轮变化的内容应该放进 view injector。
- **注入的 view entries 对持久化不可见**，这是设计。某个提醒如果之后必须可审计，就把它做成
  工具结果。

## 延伸阅读

- [Todo](todo.md) · [Skills](skills.md) · [MCP](mcp.md) · [记忆](memory.md)：深入了解内置插件
- [上下文管理](context.md)：view 如何围绕 injector 组装
- 示例：[`21_todos.py`](../../examples/21_todos.py)，
  [`25_custom_plugin.py`](../../examples/25_custom_plugin.py)
