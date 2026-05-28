# lovia

一个轻量、与厂商解耦的 Python Agent 框架。

[English](./README.md)

```python
from lovia import Agent, Runner

agent = Agent(name="Greeter", instructions="用一行话回复。", model="gpt-4o-mini")
result = await Runner.run(agent, "用三种语言说你好。")
print(result.output)
```

lovia 的核心由一组正交的小组件构成：`Agent` 配置、驱动循环的 `Runner`、
`Provider` Protocol，以及基于 Item 的对话记录。Provider 层支持 OpenAI Chat
Completions、OpenAI Responses API、Anthropic，以及任何 OpenAI 兼容端点。其他
能力 —— 工具、结构化输出、会话、handoff、guardrail、人工审批、MCP、Skill、
Memory、Tracing —— 都是可选的。

- **没有 DSL，没有 graph，没有隐式全局状态。** 纯 Python，带类型提示。
- **核心只依赖** `httpx` 和 `pydantic`。
- **异步优先**；同步辅助函数只在能明显简化使用的地方提供。

---

## 安装

```bash
pip install lovia                 # 核心
pip install "lovia[mcp]"          # + Model Context Protocol 客户端
pip install "lovia[web]"          # + FastAPI / SSE + 内置聊天 UI
pip install "lovia[dev]"          # + pytest, ruff, mypy
```

需要 Python 3.10+。

---

## 快速上手

一个带工具的完整 Agent，单文件：

```python
import asyncio
from lovia import Agent, Runner, tool

@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

agent = Agent(
    name="Calc",
    instructions="Use the add tool when the user asks for arithmetic.",
    model="gpt-4o-mini",
    tools=[add],
)

async def main() -> None:
    result = await Runner.run(agent, "What is 17 + 25?")
    print(result.output)

asyncio.run(main())
```

`Runner.run` 返回 `RunResult`，包含最终的 `output`、新增的 `new_items`、token
`usage` 与 `turns`。若需要流式输出，改用 `Runner.stream(agent, ...)` 并迭代
事件流。

---

## 核心概念

### Agent

`Agent` 是一个 dataclass —— 一份静态配置：

```python
Agent(
    name="Researcher",
    instructions="...",         # 字符串，或 function(ctx) -> str
    model="gpt-4o-mini",        # provider:model，或仅 model（默认 openai:）
    tools=[...],                # list[Tool]
    output_type=MyModel,        # 可选：用于结构化输出的 pydantic 模型
    handoffs=[...],             # 可移交给的其他 Agent
    input_guardrails=[...],     # 在循环开始前校验输入
    output_guardrails=[...],    # 在返回前校验最终输出
    hooks=...,                  # 用于观测的 AgentHooks
    model_settings=ModelSettings(temperature=0.2, ...),
)
```

`Agent` 是 `Generic[TContext]`。把 `context=` 传给 `Runner.run`，第一个参数
类型为 `RunContext[TContext]` 的工具就能拿到这个 context。

### Runner

`Runner` 是无状态的编排器，两个入口：

- `await Runner.run(agent, input, *, context=None, session=None, ...)` ——
  阻塞版本，返回 `RunResult`。
- `Runner.stream(agent, input, ...)` 返回 `RunHandle`。用 `async for event in
  handle.events()` 接收结构化事件（`TextDelta`、`ToolCallStarted`、
  `MessageCompleted` …），用 `await handle.result()` 获取最终 `RunResult`。

### 工具

用 `@tool` 装饰器声明工具。类型注解被转成 JSON Schema，docstring 成为
描述。

```python
from dataclasses import dataclass
from lovia import RunContext, tool

@dataclass
class Deps:
    db: "Database"

@tool
async def lookup(ctx: RunContext[Deps], user_id: str) -> dict:
    """Look up a user by id."""
    return await ctx.context.db.get(user_id)
```

工具策略作为 `@tool` 的扁平字段：

```python
@tool(
    needs_approval=True,         # 经 ApprovalChannel 审批
    retries=2,                   # 工具异常时重试
    timeout=10.0,                # 单次调用超时（秒）
    result_renderer=lambda r: r.summary,  # 将结果格式化给模型看
    wrap=my_middleware,          # 逃生口：(next, args, ctx) -> result
)
def risky(...): ...
```

也可以直接构造 `Tool(name=..., parameters=..., invoke=...)`。

### Items：对话记录

一次运行产生一串带类型的 *items*，这是规范化的会话记录：

- `InputMessageItem` —— 用户 / 系统输入。
- `MessageOutputItem` —— 助手文本。
- `ReasoningItem` —— 模型推理内容（OpenAI Responses 等）。
- `ToolCallItem` / `ToolCallOutputItem` —— 工具调用及其结果。

Items 是 dataclass，附带稳定的 `to_dict` / `from_dict`，适合持久化。如果
你想要 Chat 风格的视图，`result.messages` 会从 items 派生出来。

### Provider

`Provider` 把厂商 API 适配到 lovia 的 Item 流协议。`Agent(model=...)` 传入
的字符串决定使用哪个 Provider：

| 前缀 | 适配 |
| --- | --- |
| *(无)* 或 `openai:` | OpenAI Chat Completions |
| `openai-responses:` / `responses:` | OpenAI Responses API（reasoning items、server tools） |
| `anthropic:` | Anthropic Messages |
| 自定义前缀 | 你注册的任意实现 |

OpenAI 兼容端点（DeepSeek、Ollama、vLLM 等）显式构造 provider：

```python
from lovia import OpenAIChatProvider

provider = OpenAIChatProvider(
    model="deepseek-chat",
    base_url="https://api.deepseek.com/v1",
    api_key=os.environ["DEEPSEEK_API_KEY"],
)
agent = Agent(name="...", model=provider)
```

自定义 Provider 实现 `Provider.stream(input: list[Item], ...) ->
AsyncIterator[ItemDelta]`，整个协议就这一条。

### 结构化输出

把 `output_type=` 设为一个 Pydantic 模型，`result.output` 就是该模型的实例。
lovia 负责生成 JSON Schema、补充 prompt，并在模型返回非法 JSON 时执行一次
修复尝试。需要自定义修复行为时，传入 `OutputRepairStrategy`。

### Session

`Session` 在多轮对话之间持久化 items：

```python
from lovia.stores import SQLiteSession

session = SQLiteSession(path="chat.db", session_id="user-42")
await Runner.run(agent, "我刚才问了什么？", session=session)
```

内置：`InMemorySession`、`SQLiteSession`。`Session` Protocol 只有两个方法
（`load` / `append`），需要 Redis、Postgres 等自行实现即可。

### Checkpoint 与恢复

`Runner.stream(..., checkpointer=...)` 在每轮结束后保存 `RunSnapshot`。之后
用 `Runner.resume(snapshot, ...)` 恢复。适合长流程和人工审批场景。

### 多 Agent：handoff 与 agent-as-tool

两种正交模式，都是一等公民，没有 graph DSL：

- **Handoff.** 当前 Agent 把控制权移交给另一个 Agent，常用于 triage / 专家
  分发。
  ```python
  triage = Agent(name="Triage", handoffs=[Handoff(refunds), Handoff(billing)])
  ```
- **Agent-as-tool.** 把另一个 Agent 当作函数调用：
  ```python
  summarizer = Agent(name="Summarizer", ...)
  writer = Agent(name="Writer", tools=[agent_as_tool(summarizer, name="summarize")])
  ```

### Hooks 与事件

通过订阅事件或挂载 `AgentHooks` 观测运行过程。事件流与流式消费者读取的是
同一份；hooks 和 tracer 只是另开一条订阅通道。

```python
from lovia import AgentHooks, events as ev

hooks = AgentHooks()
hooks.on(ev.ToolCallStarted, lambda e: print("tool:", e.call.name))
agent = Agent(..., hooks=hooks)
```

### 审批

人工审批：把工具标记为 `needs_approval=True`，并提供 `ApprovalChannel`。
运行会在 `ApprovalRequired` 事件处暂停，通过 channel 决定继续或拒绝。

### 安全护栏

- `RunBudget(max_turns=..., max_tokens=..., wall_clock=...)` —— 硬上限。
- `RetryPolicy` —— 对 provider 错误退避重试，可配置 fallback provider。
- `CancelToken` —— 协作式取消正在运行的请求。
- `InputGuardrail` / `OutputGuardrail` —— 抛 `GuardrailTripped` 的校验器。

### Tracing

核心自带 `ConsoleTracer` 和 `InMemoryTracer`；默认是 `NoopTracer`。每次
run / turn / tool / handoff / model-call 自动生成 span。

```python
from lovia import ConsoleTracer
agent = Agent(..., tracer=ConsoleTracer())
```

需要接入 OpenTelemetry 时，写一个薄薄的 `Tracer` 适配即可 —— Protocol 只有
三个方法。

### MCP、Skills、Memory

- **MCP** (`lovia[mcp]`)：连接 Model Context Protocol 服务器，把其工具暴露
  给 Agent。
- **Skills**：从目录中懒加载 prompt 片段（`SKILL.md` + 资源），通过
  `SkillCatalog` 提供。
- **Memory**：长期检索 Protocol，与 `Session` 解耦。核心只提供协议，后端
  自带。

### ContextPolicy：多轮会话不崩

长会话迟早会撞上模型的上下文窗口。`ContextPolicy` 在每次 LLM 调用前重写
transcript，把过老的内容摘要掉，让对话可以无限继续下去。

- **默认**：不传 `context_policy` 时行为不变，零开销。
- **开箱即用**：`SummarizingContextPolicy` 提供两层兜底——一旦预估 prompt
  超过 `compact_at_ratio * max_tokens`（默认 0.8）就让 LLM 生成摘要；如果
  provider 已经返回 `ContextOverflowError`（HTTP 400 "prompt is too long" 等），
  policy 会被反应式触发、用更激进的 tail 重压一次再重试一次。
- **三层正交**：
  - `Session`：活跃 transcript（压缩后写回）
  - `archive` 回调：压缩前的全量快照，仅用于审计/回放
  - `Memory`：跨会话的语义知识，在 `ContextCompacted` 事件里手动联动

```python
from lovia import (
    Agent, Runner, SummarizingContextPolicy, ProviderSummarizer
)

policy = SummarizingContextPolicy(
    # 不传 max_tokens 时回落到 provider.context_window(model)；都拿不到就
    # 只走反应式 413 兜底。
    keep_recent_messages=10,
    # 想省钱可指定独立的小模型做摘要：
    summarizer=ProviderSummarizer(provider=OpenAIChatProvider("gpt-4o-mini")),
    # 一行 lambda 备份全量历史：
    archive=lambda ev: open(f"archive/{ev.session_id}.jsonl", "a").write(...),
)

await Runner.run(agent, "...", session=sess, session_id="u1",
                 context_policy=policy)
```

如果需要不同策略，实现 `ContextPolicy` 协议（两个方法：`apply` 和
`apply_reactive`）即可。

---

## 示例

均可从仓库根目录运行（多数需要 `OPENAI_API_KEY`）：

| 文件 | 主题 |
| --- | --- |
| `01_hello.py` | 最小 Agent。 |
| `02_tools.py` | 工具调用。 |
| `03_streaming.py` | 事件流消费。 |
| `04_structured_output.py` | Pydantic `output_type`。 |
| `05_handoff.py` | 分诊到专家 Agent。 |
| `06_agent_as_tool.py` | 一个 Agent 作为另一个的工具。 |
| `07_session.py` | 使用 `SQLiteSession` 的多轮对话。 |
| `08_skills.py` | 基于文件系统的 Skill。 |
| `09_compat_provider.py` | DeepSeek / Ollama / vLLM 等 OpenAI 兼容端点。 |
| `10_hooks.py` | 用 `AgentHooks` 观测。 |
| `11_approval.py` | 人工审批。 |
| `12_multimodal.py` | 图文输入。 |
| `13_budget_and_cancel.py` | 预算、重试、取消、fallback。 |
| `14_guardrails.py` | 输入 / 输出 guardrail。 |
| `15_resume.py` | Checkpoint 与恢复。 |
| `16_web_serve.py` | 通过 SSE 暴露内置聊天 UI。 |
| `17_responses_reasoning.py` | OpenAI Responses + reasoning items。 |
| `18_context_policy.py` | 长会话用 `SummarizingContextPolicy` 自动压缩。 |

---

## 状态

预 1.0 阶段。公开 API 由 `lovia/__init__.py` 中的导出决定，其他都是内部
实现，可能随时变更。框架尚未发布，仍在密集调整设计；该阶段不会为 break
change 引入兼容垫层。

## 许可

MIT。
