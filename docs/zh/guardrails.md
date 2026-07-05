# 护栏

有些规则不应该被模型提示词绕过。guardrail 是带否决权的程序化检查：
**输入护栏**在第一次模型调用前检查对话；**输出护栏**在最终答案返回前检查答案。

```python
from lovia import Agent
from lovia.exceptions import GuardrailTripped


async def no_email_addresses(messages, ctx):
    if any("@" in str(m.content) for m in messages):
        raise GuardrailTripped("不允许包含邮箱地址。")


async def must_cite(output, ctx):
    if "source:" not in str(output).lower():
        return "缺少来源引用。"


agent = Agent(
    name="researcher",
    model="openai:gpt-5.5",
    input_guardrails=[no_email_addresses],
    output_guardrails=[must_cite],
)
```

## 契约

guardrail 是任意 callable，同步或异步都可以：

- **输入**：以 `fn(messages, ctx)` 调用，`messages` 是完整初始 transcript 的 chat 格式 view
  （system prompt、session 历史、本次输入）。只在第一次模型调用前运行一次。
- **输出**：以 `fn(output, ctx)` 调用，`output` 是运行的最终输出。它在解析/校验后运行，所以如果
  使用类型化 [`output_type`](structured-output.md)，你检查的是校验后的对象，不是原始文本。

用两种方式表示违规：

- **抛 `GuardrailTripped("reason")`**：显式，携带你的消息；
- **返回 truthy 值**：非空字符串会作为原因（`"output guardrail: Missing source citation."`）；
  `True` 产生通用原因。`None`、`False`、`""` 表示通过。

触发护栏会**结束运行**：`Runner.run` 抛 `GuardrailTripped`；stream 以携带该错误的 `RunFailed`
结束。没有自动重试。护栏是边界，不是提醒；如果想“再试一次”，请捕获异常后重跑，或者在开发期把规则
写成 [eval check](eval.md)。

两种护栏都会收到实时 `ctx`（[`RunContext`](concepts.md#runcontext唯一的运行句柄)），所以检查可以感知
tenant（`ctx.deps`）、usage（`ctx.usage`）或 transcript（`ctx.entries`）。护栏按列表顺序运行，
第一个违规胜出。[插件](plugins.md)也可以贡献护栏；它们在同样的检查点运行，和 agent 自己的护栏合并，
中止仍由循环负责，而不是插件负责。

## 配方

**用便宜模型筛查**：guardrail 是 async，所以可以调用自己的分类器：

```python
screen = Agent(name="screen", model="openai:gpt-5.5-mini", output_type=bool,
               instructions="如果请求在寻求法律建议，回答 true。")

async def no_legal_advice(messages, ctx):
    result = await screen.run(str(messages[-1].content))
    if result.output:
        return "我们不能提供法律建议。"
```

**强制 schema 表达不了的输出不变量**：必须有引用、禁用短语、最大长度：

```python
async def short_enough(output, ctx):
    if len(str(output)) > 2_000:
        return "回答超过 2,000 字符限制。"
```

**想脱敏而不是拒绝？** 护栏只有通过/失败，不能改写值。脱敏应该放在数据流经的位置：
工具参数/结果用[工具策略](tools.md#工具策略)，输入用你自己的预处理。

## 容易踩的点

- **输入护栏看到历史，而不只是新消息。** “拒绝任何 @ 符号”这种规则会因为三轮前的消息触发，
  即使当时是合法的。只想检查新输入时，请看 `messages[-1]`。
- **输出护栏不会在 [checkpoint 重放](sessions-and-checkpoints.md#run_id-是幂等键)时运行。**
  它们已经在原始完成时运行过；重放直接返回已存结果。
- **护栏延迟就是运行延迟。** 输入护栏在第一次模型调用前执行；LLM 筛查护栏会增加一次完整往返。
  把快检查放在列表前面。
- **中途内容不在护栏范围内**，这是设计。要管单个工具调用，请用[审批](human-in-the-loop.md)或工具策略；
  要管流式文本，请在消费者里过滤。

## 延伸阅读

- [人工介入](human-in-the-loop.md)：每个调用的门禁
- [评测](eval.md)：输出护栏在开发期的对应物
- 示例：[`13_guardrails.py`](../../examples/13_guardrails.py)
