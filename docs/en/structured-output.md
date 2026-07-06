# Structured output

"Return JSON" prompts drift: the model adds prose, wraps output in code
fences, renames a field. `output_type` replaces hope with a contract — the
run's final answer is parsed and validated into the type you declared, or
the run fails loudly after a bounded repair attempt.

```python
from pydantic import BaseModel

from lovia import Agent, Runner


class Brief(BaseModel):
    title: str
    bullets: list[str]


agent = Agent(name="summarizer", model="glm-5.2", output_type=Brief)

result = await Runner.run(agent, "Summarize lovia for a Python developer.")
print(result.output.title)          # typed access — result.output is a Brief
```

## Accepted types

Anything lovia can build a JSON Schema for and coerce into:

- Pydantic models (the richest option: constraints, custom validators)
- dataclasses and `TypedDict`s
- plain types and containers — `list[str]`, `dict[str, int]`,
  `Literal[...]`, unions, `int`, `bool`, ...
- `str` — the default, meaning free-form text with no parsing at all

The model may still call tools on the way; the contract applies to the
**final** message that ends the run.

## Per-run override

`output_type` on the agent is the default; a run can override it:

```python
result = await Runner.run(agent, "Return a launch checklist.", output_type=list[str])
```

The override is run-wide: after a [handoff](multi-agent.md), the target
agent inherits it too (without an override, each agent uses its own
declared `output_type`).

## How the schema reaches the model

Two strategies, chosen automatically per provider:

- **Native** — providers with structured-output support (`OpenAI
  response_format`, Anthropic's output format) receive the JSON Schema in
  the request and enforce it server-side.
- **Prompt fallback** — for everything else, lovia appends an "Output
  format" block to the system prompt instructing the model to reply with a
  single schema-shaped JSON document. It lives in the system prompt (not a
  synthetic tool) so the requirement stays visible regardless of context
  length or tool count.

Either way, parsing is **lenient** before it is strict: the raw text is
tried as-is, then with markdown code fences stripped, then as the first
balanced JSON object/array embedded in surrounding prose. Only then is the
parsed document validated against your type.

## Repair

When the final message fails to parse or validate, the agent's
`output_repair` policy decides what happens next:

- **`True` (default)** — the runner appends a corrective user prompt
  (quoting the validation error) and lets the model try once more. A second
  failure raises.
- **`False`** — fail fast: raise `OutputValidationError` immediately.
- **An `OutputRepairStrategy`** — your own policy:

  ```python
  class PatientRepair:
      def build_prompt(self, exc, attempt):
          if attempt > 3:
              return None            # give up: the error is re-raised
          return f"Attempt {attempt} failed: {exc}. Reply with only the JSON."

  agent = Agent(..., output_type=Brief, output_repair=PatientRepair())
  ```

  `build_prompt` receives the `OutputValidationError` and the 1-based
  attempt number; returning `None` stops retrying. Each repair consumes a
  normal turn (it counts toward `max_turns` and the budget).

`OutputValidationError` carries `raw` (a snippet of what the model actually
said) and `output_type_name` — enough to debug a persistent mismatch from
logs alone.

## Sharp edges

- **`output_type=str` means "no contract"**, not "validate it's a string".
  Everything is a string then; repair never triggers.
- **Schema complexity costs accuracy.** Deeply nested unions and
  open-ended `dict[str, Any]` fields degrade model compliance long before
  they break the parser — flat, explicit models with field descriptions
  validate best.
- **An empty reply goes through repair too** (there's nothing to parse).
  If you see repair loops ending in `OutputValidationError` with empty
  `raw`, check `finish_reason` — it's usually `max_tokens` truncation, not
  disobedience.

## See also

- [Agents](agents.md) — where `output_type` and `output_repair` live
- [Providers & models](providers.md) — which providers get the native path
- Example: [`04_structured_output.py`](../../examples/04_structured_output.py)
