# 测试

Agent 代码也应该有离线、免费、确定性的测试。需要网络、会抖、每次都花钱的“测试”最后没人会跑。
`lovia.testing` 提供了让这件事变成日常的 test double：`ScriptedProvider`，一个真正的
`Provider`，会回放预先写好的 turn。

```python
from lovia import Agent, tool
from lovia.testing import ScriptedProvider, call, text


@tool
def add(a: int, b: int) -> int:
    """把两个数相加。"""
    return a + b


def make_agent() -> Agent:
    return Agent(
        name="calc",
        model=ScriptedProvider([
            call("add", {"a": 2, "b": 3}, call_id="c1"),   # turn 1：请求工具
            text("答案是 5。"),                              # turn 2：最终答案
        ]),
        tools=[add],
    )


async def test_calc_uses_the_tool():
    result = await make_agent().run("2 + 3 等于多少？")
    assert result.output == "答案是 5。"
    assert result.turns == 2
```

script 是模型这一侧的对话：每个条目按顺序回应一次模型调用。真实工具会真实运行，只有 LLM 被脚本替代；
所以测试会覆盖**真实循环**：schema 校验、并发执行、审批门禁、结构化输出解析、session 持久化。

## 构建脚本

| Helper | 产出 |
| --- | --- |
| `text("Done.")` | 纯文本 turn（按字符流式输出） |
| `text("Done.", reasoning="hmm...")` | 带 reasoning delta 的文本，用来测试 `ReasoningDelta` 消费者 |
| `call("search", {"q": "tides"})` | 请求一个工具调用的 turn（`call_id` 默认是 `call_<name>`） |
| `batch(("a", {...}), ("b", {...}))` | 同一 turn 请求多个调用，用来测试[并发执行](tools.md#并发执行与屏障) |

脚本耗尽会抛 `AssertionError("ScriptedProvider ran out of canned responses")`，错误 turn 数会明确失败，
而不是挂住。

## 断言 agent 看到了什么

provider 会记录它收到的每个 prompt：

```python
provider = ScriptedProvider([text("ok")])
agent = Agent(name="bot", model=provider, instructions="回答要简短。")
await agent.run("hello")

first_prompt = provider.calls[0]              # turn 1 的输入，list[Message]
assert first_prompt[0].role == "system"
assert "回答要简短。" in first_prompt[0].content
```

`provider.calls[i]` 是第 *i* 个 turn 输入的 chat 格式 view。它非常适合测试
[动态 instructions](agents.md#instructions)、[view injectors](plugins.md#view-injector每轮插入点)，以及
[compaction](context.md) 行为，比如“被清理的结果真的离开 view 了吗？”。

## 测什么，用什么测

- **工具本身**：普通 pytest；`@tool` 函数本质上仍是函数。
- **循环行为**（路由、工具选择、修复、护栏、handoff）：用上面的 `ScriptedProvider`。
  handoff 和 [agent-as-tool](multi-agent.md) 子运行各自从自己的 agent provider 消费脚本；
  请给每个 agent 自己的 script。
- **事件消费者 / UI**：脚本化一次运行并迭代 `Runner.stream(...)`；delta 按字符流出，消费者会看到真实的碎片化。
- **行为质量**（“答得好吗？”）：用 [eval](eval.md)。eval 的离线模式也使用同一个
  `ScriptedProvider`，live 模式用真实模型。
- **Live smoke tests**：打标，默认跳过，按需运行。本仓库使用 `pytest -m live_provider`，
  并由 `LOVIA_LIVE_TESTS=1` gate。

## 容易踩的点

- **`ScriptedProvider` 是一次性的。** 它会从共享队列 pop，不可重复，也不并发安全。每次运行都创建新的
  provider（和 agent）；在 `evaluate()` 里传 agent **工厂**正是这个原因。
- **`supports_json_schema` 是 `False`**，所以[结构化输出](structured-output.md)走 prompt 路径：
  scripted 最终 turn 必须是 JSON 文档本身（`text('{"title": "..."}')`），schema instructions 会落在
  `provider.calls[0][0]`，你可以对它断言。
- **异步测试需要异步 runner。** 本仓库使用 `pytest-asyncio`；没有运行中事件循环的普通测试里，
  `Runner.run_sync` 也可以用。

## 延伸阅读

- [评测](eval.md)：同一个 double，用来度量质量而不是接线
- [Provider](providers.md#自定义-provider)：`ScriptedProvider` 也是参考 `Provider` 实现
- 示例：[`10_custom_provider.py`](../../examples/10_custom_provider.py)（离线），以及本仓库的 `tests/` 目录
