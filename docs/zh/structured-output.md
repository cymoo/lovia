# 结构化输出

“请返回 JSON”这种 prompt 并不可靠：模型可能会加解释文字、套代码块，甚至改字段名。
`output_type` 用契约代替期望：运行的最终答案会被解析并校验成你声明的类型；如果失败，
会在有边界的修复尝试后明确报错。

```python
from pydantic import BaseModel

from lovia import Agent, Runner


class Brief(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(name="summarizer", model="glm-5.2", output_type=Brief)

result = await Runner.run(agent, "给 Python 开发者总结 lovia。")
print(result.output.title)          # 类型化访问：result.output 是 Brief
```

## 接受哪些类型

任何 lovia 能构建 JSON Schema、并能转换为目标值的类型都可以：

- Pydantic model（能力最强：约束、自定义 validator）
- dataclass 和 `TypedDict`
- 普通类型和容器，如 `list[str]`、`dict[str, int]`、`Literal[...]`、union、
  `int`、`bool` 等
- `str`：默认值，表示自由文本，不做任何解析

运行过程中模型仍然可以调用工具；契约只作用于结束运行的**最终**消息。

## 每次运行覆盖

agent 上的 `output_type` 是默认值；某次运行也可以覆盖：

```python
result = await Runner.run(agent, "返回一份发布 checklist。", output_type=list[str])
```

覆盖项作用于整次运行。经过 [handoff](multi-agent.md) 后，目标 agent 也继承这个覆盖；
如果没有覆盖，每个 agent 使用自己声明的 `output_type`。

## Schema 如何到达模型

lovia 会按 provider 自动选择两种策略：

- **原生接口**：支持结构化输出的 provider（OpenAI `response_format`、Anthropic
  output format）会在请求里收到 JSON Schema，并由服务端约束。
- **Prompt fallback**：其他 provider 会让 lovia 在 system prompt 后追加一个
  “Output format” 块，要求模型只回复一个符合 schema 的 JSON 文档。它放在 system
  prompt 中，而不是合成工具里，所以不管上下文长度和工具数量如何，这个要求都能保持可见。

无论哪种方式，解析都会先宽松、再严格：先尝试原始文本，再剥掉 markdown code fence，
再从周围说明文字中提取第一个平衡的 JSON object/array。之后才按你的类型校验。

## 修复

最终消息解析或校验失败时，agent 的 `output_repair` 策略决定下一步怎么做：

- **`True`（默认）**：runner 追加一条纠正用的用户 prompt（包含校验错误），让模型再试
  一次。第二次失败才抛异常。
- **`False`**：快速失败，立即抛 `OutputValidationError`。
- **一个 `OutputRepairStrategy`**：你自己的策略：

  ```python
  class PatientRepair:
      def build_prompt(self, exc, attempt):
          if attempt > 3:
              return None            # 放弃：重新抛出错误
          return f"第 {attempt} 次失败：{exc}。只回复 JSON。"

  agent = Agent(..., output_type=Brief, output_repair=PatientRepair())
  ```

  `build_prompt` 接收 `OutputValidationError` 和 1-based 尝试次数；返回 `None`
  表示停止重试。每次修复都会消耗一个正常 turn（计入 `max_turns` 和预算）。

`OutputValidationError` 携带 `raw`（模型实际输出的片段）和 `output_type_name`，
通常足够你只靠日志定位长期不匹配的问题。

## 容易踩的点

- **`output_type=str` 表示“没有契约”**，不是“校验它是字符串”。此时一切都是字符串，
  修复永远不会触发。
- **schema 越复杂，模型越容易不稳定。** 深层嵌套 union 和开放式
  `dict[str, Any]` 字段会在解析器出问题前就降低模型遵从度。扁平、明确、带字段描述的
  model 最容易通过校验。
- **空回复也会进入修复流程**（因为没有内容可解析）。如果你看到修复循环最后以
  `OutputValidationError` 结束，且 `raw` 为空，请检查 `finish_reason`，通常是
  `max_tokens` 截断，不是模型不听话。

## 延伸阅读

- [Agent](agents.md)：`output_type` 和 `output_repair` 放在哪里
- [Provider 与模型](providers.md)：哪些 provider 走原生接口
- 示例：[`04_structured_output.py`](../../examples/04_structured_output.py)
