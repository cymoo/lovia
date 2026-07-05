# Evals

Unit tests assert wiring; they can't tell you whether the agent *behaves* —
answers correctly, calls the right tool, stays concise — especially when
behavior is non-deterministic. `lovia.eval` turns "does my agent behave?"
into a declarative suite with three ideas: a **`Case`** pairs an input with
checks; a **check** is any callable over a `RunResult`; **`evaluate()`**
returns a `Report` you can print, assert on, and diff against a baseline.

```python
from lovia.eval import Case, contains, evaluate, llm_judge, tool_called

cases = [
    Case("What is the capital of France?", checks=[contains("Paris")]),
    Case("What's 23.4 * 91?", checks=[tool_called("calculator")]),
    Case(
        "Write a haiku about spring",
        checks=[llm_judge("A 5-7-5 haiku that evokes spring")],
        samples=4,               # non-determinism is measured, not retried away:
        pass_threshold=0.75,     # pass if at least 3 of 4 samples pass
    ),
]

report = await evaluate(agent, cases)
print(report)
assert report.passed
```

```
eval: 2/3 cases passed (67%) · 6 samples · 4,812 tokens · 21.4s
  ✓ What is the capital of France?  1/1
  ✓ What's 23.4 * 91?               1/1
  ✗ Write a haiku about spring      2/4  llm_judge (score 0.55) — third line has eight syllables
```

## Cases

| `Case` field | Default | Meaning |
| --- | --- | --- |
| `input` | required | a string, or `list[Message]` |
| `checks` | `()` | the pass criteria (all must pass for a sample to pass) |
| `name` | derived from input | the report label and the `compare()` join key |
| `samples` | `1` | run the case N times; non-determinism becomes a number |
| `pass_threshold` | `1.0` | fraction of samples that must pass for the case to pass |
| `context` | `None` | forwarded as the run's deps |
| `output_type` / `max_turns` | agent's / `50` | per-case run settings |
| `model` | agent's | clone the agent onto another model for this case |
| `timeout` | `None` | wall-clock cap per sample — a timeout is a failed sample, not a crashed suite |
| `metadata` | `{}` | carried untouched into the result |

`Case(model=...)` is the workhorse: live suites pin a case to a different
model; **offline suites give every case its own scripted transcript**:

```python
from lovia.testing import ScriptedProvider, call, text

Case(
    "What is 2 + 3?",
    checks=[contains("5"), tool_called("add")],
    model=ScriptedProvider([call("add", {"a": 2, "b": 3}), text("2 + 3 = 5")]),
)
```

The agent is cloned per sample when `model=` is set, so single-use scripted
providers work with `samples > 1` only via a factory (below).

## Checks

Any callable `(RunResult) -> CheckResult | bool`, sync or async — built-in
matchers, the LLM judge, and your own functions are all the same thing:

```python
def concise(result) -> bool:
    return len(str(result.output)) < 400
```

A check that raises fails *itself* (with the exception as the reason),
never the suite — `run_check` normalizes every outcome into a
`CheckResult(name=..., passed=..., score=..., reason=...)`; return one
yourself for graded results.

Built-ins: `contains(value, ignore_case=False)` / `not_contains`,
`regex(pattern)`, `equals(value)`, `matches(spec)` (recursive
subset-match for structured output — extra fields ignored, list lengths
exact; or pass a predicate), `tool_called(name)` / `tool_not_called(name)`,
`max_turns(n)`, `max_tokens(n)`, `no_error()` (no failed tool calls in the
run). Compose with `all_of(...)`, `any_of(...)`, and
`weighted({check: weight, ...}, threshold=0.7)` — weighted converts child
scores (or pass/fail) into one graded verdict.

### The LLM judge

`llm_judge(rubric, *, model=None, threshold=0.7)` grades semantics a
matcher can't:

```python
llm_judge("A polite, actionable reply that proposes a concrete next step.")
```

Under the hood it is just another check running just another agent
(`output_type=Verdict{score, reasoning}`, temperature 0). The judge model
resolves from `model=` or `$LOVIA_EVAL_JUDGE_MODEL` — never silently from
the agent under test — and `passed = score >= threshold`. Pass a
`ScriptedProvider` as `model` and the judge is offline too, which is how
the whole suite runs in CI for free.

## Running suites

```python
report = await evaluate(agent_or_factory, cases, concurrency=4, fail_fast=False,
                        price=lambda u: u.input_tokens * 3e-6 + u.output_tokens * 15e-6)
```

- **Agent or factory** (the `AgentSource` union). A zero-arg factory is
  invoked per *sample* — pass one whenever the agent is stateful (scripted
  providers, stateful tools).
- **Concurrency is across cases** (default 4); a case's samples run
  sequentially; a sample's checks run concurrently.
- **Errors are data.** A sample that raises (or times out) records its
  `error` and fails alone; the suite always completes. `fail_fast=True`
  runs cases sequentially and stops after the first failing *case*.
- **`price=`** turns usage into cost; the report then shows `· $0.0421`.

## Reports and baselines

A `Report` holds one `CaseResult` per case, each holding one
`SampleResult` per sample (checks, output, usage, latency, optional cost,
error). `Report.passed`, `.pass_rate`, and per-case `CaseResult.pass_rate`
/ `.pass_at_k(k)` (the unbiased estimator) cover the numbers;
`print(report)` gives the summary above. For CI:

```python
report.save("eval-baseline.json")            # once, on a good run

current = await evaluate(agent, cases)
diff = current.compare(Report.load("eval-baseline.json"))
print(diff)                                   # regressions / improvements / added / removed
assert diff.ok                                # truthy ⇔ no regressions
```

`compare` returns a `Diff` and joins by case **name** (duplicate names are
an error — name your cases when inputs repeat); improvements and
added/removed cases are reported but don't fail `diff.ok`.

## Sharp edges

- **Judge cost scales as `samples × judge-checks × cases`** — each judge
  evaluation is a model call. Keep judges on the cases that need semantics;
  matchers are free.
- **A ready `Agent` instance is reused across samples** unless `model=` is
  set or a factory is passed. Fine for stateless agents; wrong for scripted
  ones — the second sample finds an empty script.
- **`samples` measures, it doesn't fix.** `pass_threshold < 1.0` is a
  statement about acceptable flakiness; if a case needs retries to pass,
  that's a finding, not noise.
- **`no_error()` sees tool errors only.** A run that *raises* never reaches
  checks — it's already a failed sample with `error` set.

## See also

- [Testing](testing.md) — `ScriptedProvider`, the offline engine under
  both suites and judges
- [Guardrails](guardrails.md) — the runtime twin of an output check
- Example: [`28_eval.py`](../../examples/28_eval.py) — a fully offline,
  scripted suite with a scripted judge
