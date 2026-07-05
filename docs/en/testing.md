# Testing

Agent code deserves tests that run offline, free, and deterministically —
network-flaky "tests" that cost money per run don't get run.
`lovia.testing` ships the test double that makes this routine:
`ScriptedProvider`, a real `Provider` that replays pre-canned turns.

```python
from lovia import Agent, tool
from lovia.testing import ScriptedProvider, call, text


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


def make_agent() -> Agent:
    return Agent(
        name="calc",
        model=ScriptedProvider([
            call("add", {"a": 2, "b": 3}, call_id="c1"),   # turn 1: request the tool
            text("The answer is 5."),                       # turn 2: final answer
        ]),
        tools=[add],
    )


async def test_calc_uses_the_tool():
    result = await make_agent().run("What is 2 + 3?")
    assert result.output == "The answer is 5."
    assert result.turns == 2
```

The script is the model's side of the conversation: each entry answers one
model call, in order. Real tools run for real — only the LLM is scripted —
so this exercises the *actual loop*: schema validation, parallel execution,
approval gates, structured-output parsing, session persistence.

## Building scripts

| Helper | Produces |
| --- | --- |
| `text("Done.")` | a plain-text turn (streams character by character) |
| `text("Done.", reasoning="hmm...")` | text preceded by reasoning deltas — for testing `ReasoningDelta` consumers |
| `call("search", {"q": "tides"})` | a turn requesting one tool call (`call_id` defaults to `call_<name>`) |
| `batch(("a", {...}), ("b", {...}))` | a turn requesting several calls at once — for testing [parallel execution](tools.md#parallel-execution-and-barriers) |

A script that runs dry raises
`AssertionError("ScriptedProvider ran out of canned responses")` — a wrong
turn count fails loudly instead of hanging.

## Asserting on what the agent saw

The provider records every prompt it received:

```python
provider = ScriptedProvider([text("ok")])
agent = Agent(name="bot", model=provider, instructions="Be terse.")
await agent.run("hello")

first_prompt = provider.calls[0]              # list[Message] for turn 1
assert first_prompt[0].role == "system"
assert "Be terse." in first_prompt[0].content
```

`provider.calls[i]` is the chat-format view of turn *i*'s input — the
sharpest tool for testing [dynamic instructions](agents.md#instructions),
[view injectors](plugins.md#view-injectors-the-per-turn-seam), and
[compaction](context.md) behavior ("did the cleared result really leave
the view?").

## What to test, and with what

- **Tools in isolation** — plain pytest; a `@tool` function is just a
  function.
- **Loop behavior** (routing, tool choice, repair, guardrails, handoff) —
  `ScriptedProvider`, as above. Handoffs and
  [agent-as-tool](multi-agent.md) sub-runs each consume from their *own*
  agent's provider, so give each agent its own script.
- **Event consumers / UIs** — script a run and iterate
  `Runner.stream(...)`; deltas stream character-by-character so consumers
  see realistic fragmentation.
- **Behavioral quality** ("does it *answer well*?") — that's
  [evals](eval.md), which use the same `ScriptedProvider` for their offline
  mode and real models for the live one.
- **Live smoke tests** — mark them, skip by default, run on demand (this
  repo uses `pytest -m live_provider` gated behind `LOVIA_LIVE_TESTS=1`).

## Sharp edges

- **A `ScriptedProvider` is single-use.** It pops a shared queue — neither
  repeat- nor concurrency-safe. Build a fresh provider (and agent) per run;
  with `evaluate()` pass an agent *factory* for exactly this reason.
- **`supports_json_schema` is `False`**, so
  [structured output](structured-output.md) takes the prompt path: your
  scripted final turn must be the JSON document itself
  (`text('{"title": "..."}')`), and the schema instructions land in
  `provider.calls[0][0]` where you can assert on them.
- **Async tests need an async runner** — the repo uses `pytest-asyncio`;
  `Runner.run_sync` works in plain tests when no loop is running.

## See also

- [Evals](eval.md) — the same double, measuring quality instead of wiring
- [Providers](providers.md#custom-providers) — `ScriptedProvider` doubles
  as the reference `Provider` implementation
- Example: [`10_custom_provider.py`](../../examples/10_custom_provider.py)
  (offline), plus this repo's `tests/` directory
