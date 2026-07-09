# 评测

单元测试能确认代码有没有接好，却很难判断 agent **表现**如何：答得对不对、有没有调用正确工具、
是否足够简洁。行为本身带有波动时，这个问题会更明显。`lovia.eval` 让你用 `Case` 写下输入和
验收条件；验收条件可以是普通函数，也可以是 LLM judge。`evaluate()` 会跑完整套 case，
并返回一个可打印、可断言、也可与基线对比的 `Report`。

```python
from lovia.eval import Case, contains, evaluate, llm_judge, tool_called

cases = [
    Case("法国首都是哪里？", checks=[contains("巴黎")]),
    Case("23.4 * 91 等于多少？", checks=[tool_called("calculator")]),
    Case(
        "写一首关于春天的俳句",
        checks=[llm_judge("一首 5-7-5 音节、能唤起春天意象的俳句")],
        samples=4,               # 把不确定性量出来，而不是靠重试掩盖
        pass_threshold=0.75,      # 4 个 sample 至少 3 个通过即通过
    ),
]

report = await evaluate(agent, cases)
print(report)
assert report.passed
```

```text
eval: 2/3 cases passed (67%) · 6 samples · 4,812 tokens · 21.4s
  ✓ 法国首都是哪里？              1/1
  ✓ 23.4 * 91 等于多少？          1/1
  ✗ 写一首关于春天的俳句          2/4  llm_judge (score 0.55) — 第三行有八个音节
```

## Case

| `Case` 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `input` | 必填 | 字符串，或 `list[Message]` |
| `checks` | `()` | 通过标准（一个 sample 必须全部满足） |
| `name` | 从 input 派生 | 报告标签，也是 `compare()` 的匹配键 |
| `samples` | `1` | 同一个 case 运行 N 次；把不确定性量出来 |
| `pass_threshold` | `1.0` | case 判定通过所需的 sample 通过率 |
| `context` | `None` | 作为运行 deps 转发 |
| `output_type` / `max_turns` | agent 的配置 / `50` | 每个 case 的运行设置 |
| `model` | agent 的配置 | 把 agent clone 到另一个模型上运行这个 case |
| `timeout` | `None` | 每个 sample 的实际耗时上限；timeout 会记为失败 sample，不会让整个套件中断 |
| `metadata` | `{}` | 原样携带到结果 |

`Case(model=...)` 很常用：在线评测可以把某个 case 固定到不同模型；**离线评测可以给每个 case
自己的 scripted transcript**：

```python
from lovia.testing import ScriptedProvider, call, text

Case(
    "2 + 3 等于多少？",
    checks=[contains("5"), tool_called("add")],
    model=ScriptedProvider([call("add", {"a": 2, "b": 3}), text("2 + 3 = 5")]),
)
```

设置 `model=` 时，agent 会按 sample clone。一次性 `ScriptedProvider` 如果要配合
`samples > 1` 使用，就只能通过工厂传入（见下）。

## Checks

任何 `(RunResult) -> CheckResult | bool` callable 都可以，同步或异步都行。内置匹配器、
LLM judge 和你自己的函数遵循同一套接口：

```python
def concise(result) -> bool:
    return len(str(result.output)) < 400
```

抛异常的 check 只会让**自己**失败（异常会作为原因记录），不会让整个套件中断。`run_check` 会把所有结果
规范化成 `CheckResult(name=..., passed=..., score=..., reason=...)`；需要分数型结果时，也可以自己返回一个。

内置检查：`contains(value, ignore_case=False)` / `not_contains`、`regex(pattern)`、
`equals(value)`、`matches(spec)`（结构化输出的递归子集匹配：忽略额外字段，列表长度必须精确；也可传谓词）、
`tool_called(name)` / `tool_not_called(name)`、`max_turns(n)`、`max_tokens(n)`、`no_error()`
（运行中没有失败工具调用）。可用 `all_of(...)`、`any_of(...)` 和
`weighted({check: weight, ...}, threshold=0.7)` 组合；weighted 会把子 score（或通过/失败）合成一个
带分数的判断结果。

### LLM 裁判

`llm_judge(rubric, *, model=None, threshold=0.7)` 可以评估匹配器难以表达的语义：

```python
llm_judge("礼貌、可执行，并提出一个具体下一步。")
```

底层它仍然只是一个 check，会运行另一个 agent（`output_type=Verdict{score, reasoning}`，
temperature 0）。裁判模型来自 `model=` 或 `$LOVIA_EVAL_JUDGE_MODEL`，**不会**自动使用被测 agent。
`passed = score >= threshold`。把 `ScriptedProvider` 作为 `model` 传入，裁判也可以离线运行；
这样整套评测就能在 CI 中免费跑。

## 运行套件

```python
report = await evaluate(agent_or_factory, cases, concurrency=4, fail_fast=False,
                        price=lambda u: u.input_tokens * 3e-6 + u.output_tokens * 15e-6)
```

- **Agent 或工厂**（`AgentSource` union）。零参工厂会按**每个 sample**调用；当 agent 有状态时
  （scripted provider、有状态工具），请传工厂。
- **并发发生在 case 之间**（默认 4）；同一个 case 的 samples 串行运行；同一个 sample 的 checks 并发运行。
- **错误也是数据。** sample 抛异常或 timeout 会记录自己的 `error` 并失败；套件总会跑完。
  `fail_fast=True` 会串行运行 case，并在第一个失败 **case** 后停止。
- **`price=`** 把 usage 转成成本；报告会显示 `· $0.0421`。

## 报告与基线

`Report` 里每个 case 对应一个 `CaseResult`，每个 case 下又有多个 `SampleResult`
（checks、output、usage、latency、可选 cost、error）。`Report.passed`、`.pass_rate`，以及每个 case 的
`CaseResult.pass_rate` / `.pass_at_k(k)`（无偏估计）覆盖数值指标；`print(report)` 会输出上面的摘要。
CI 中可以这样用：

```python
report.save("eval-baseline.json")            # 一次，在一个好结果上保存

current = await evaluate(agent, cases)
diff = current.compare(Report.load("eval-baseline.json"))
print(diff)                                   # regressions / improvements / added / removed
assert diff.ok                                # 为真 ⇔ 没有 regression
```

`compare` 返回 `Diff`，按 case **name** 匹配（重复名称会报错；输入重复时请手动命名 case）。
improvement 和新增/删除 case 会出现在报告里，但不会让 `diff.ok` 失败。

## 容易踩的点

- **裁判成本按 `samples × judge-checks × cases` 增长**，每次 judge 评估都是一次模型调用。
  只把 judge 用在真正需要语义判断的 case 上；匹配器是免费的。
- **现成 `Agent` 实例会在 samples 间复用**，除非设置了 `model=` 或传入工厂。无状态 agent 没问题；
  scripted agent 不适合，第二个 sample 会发现脚本已经耗尽。
- **`samples` 是度量，不是修复。** `pass_threshold < 1.0` 表达的是你能接受的波动；如果某个 case
  需要靠重试才通过，那是发现问题，不是噪声。
- **`no_error()` 只看工具错误。** 运行本身抛异常时不会进入 checks；它已经是带 `error` 的失败 sample。

## 延伸阅读

- [测试](testing.md)：`ScriptedProvider`，eval 和 judge 离线模式共用的引擎
- [护栏](guardrails.md)：输出 check 在运行时的对应物
- 示例：[`28_eval.py`](../../examples/28_eval.py)：完整的离线 scripted 评测套件，带 scripted judge
