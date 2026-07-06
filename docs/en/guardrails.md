# Guardrails

Some rules the model must not be able to break, however it is prompted.
Guardrails are programmatic checks with veto power: **input guardrails**
screen the conversation before the first model call; **output guardrails**
screen the final answer before it is returned.

```python
from lovia import Agent
from lovia.exceptions import GuardrailTripped


async def no_email_addresses(messages, ctx):
    if any("@" in str(m.content) for m in messages):
        raise GuardrailTripped("Email addresses are not allowed.")


async def must_cite(output, ctx):
    if "source:" not in str(output).lower():
        return "Missing source citation."


agent = Agent(
    name="researcher",
    model="glm-5.2",
    input_guardrails=[no_email_addresses],
    output_guardrails=[must_cite],
)
```

## The contract

A guardrail is any callable, sync or async:

- **Input**: called as `fn(messages, ctx)` with the chat-format view of the
  fully built initial transcript (system prompt, session history, your
  input) — once, before the first model call.
- **Output**: called as `fn(output, ctx)` with the run's final output —
  after parsing/validation, so with a typed
  [`output_type`](structured-output.md) you check the validated object, not
  raw text.

Signal a violation either way:

- **raise `GuardrailTripped("reason")`** — explicit, carries your message;
- **return a truthy value** — a non-empty string becomes the reason
  (`"output guardrail: Missing source citation."`); `True` produces a
  generic one. `None`, `False`, and `""` mean "pass".

A tripped guardrail **ends the run**: `Runner.run` raises
`GuardrailTripped`; a stream closes with `RunFailed` carrying it. There is
no automatic retry — a guardrail is a boundary, not a nudge (if you want
"try again", catch the exception and re-run, or express the rule as an
[eval check](eval.md) during development instead).

Both hooks receive the live `ctx`
([`RunContext`](concepts.md#runcontext-the-one-handle)), so checks can be
tenant-aware (`ctx.deps`), usage-aware (`ctx.usage`), or transcript-aware
(`ctx.entries`). Guardrails run in list order; the first violation wins.
[Plugins](plugins.md) may contribute guardrails too — they run at the same
checkpoints, merged with the agent's own, and the loop (never the plugin)
owns the abort.

## Recipes

**Screen with a separate model** — a guardrail is async, so it can run its own
scripted classifier:

```python
screen = Agent(name="screen", model="glm-5.2", output_type=bool,
               instructions="Answer true if the request asks for legal advice.")

async def no_legal_advice(messages, ctx):
    result = await screen.run(str(messages[-1].content))
    if result.output:
        return "We can't provide legal advice."
```

**Enforce output invariants** the schema can't express — citation presence,
banned phrases, max length:

```python
async def short_enough(output, ctx):
    if len(str(output)) > 2_000:
        return "Answer exceeds the 2,000-character limit."
```

**Redact instead of reject?** Guardrails are pass/fail — they cannot rewrite
values. Redaction belongs where the data flows:
[tool policies](tools.md#tool-policies) for tool arguments/results, your own
pre-processing for input.

## Sharp edges

- **Input guardrails see history too**, not just the new message — a rule
  like "reject any @-sign" trips on messages from three turns ago that were
  fine at the time. Check `messages[-1]` when you mean "the new input".
- **Output guardrails don't run on
  [checkpoint replays](sessions-and-checkpoints.md#run_id-is-an-idempotency-key)**
  — they ran on the original completion; a replay returns the stored result
  as-is.
- **Guardrail latency is run latency.** Input guardrails sit before the
  first model call; an LLM-screening guardrail adds a full round-trip.
  Keep the fast checks first in the list.
- **Mid-run content is out of scope by design** — guardrails bracket the
  run. To police individual tool calls, use
  [approval](human-in-the-loop.md) or a tool policy; to police streamed
  text, filter in your consumer.

## See also

- [Human in the loop](human-in-the-loop.md) — per-call gating
- [Eval](eval.md) — the development-time twin of output guardrails
- Example: [`13_guardrails.py`](../../examples/13_guardrails.py)
