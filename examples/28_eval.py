"""Evaluate agent behavior with ``lovia.eval``.

Three ideas cover the whole API:

* a ``Case`` pairs an input with the checks its run must satisfy;
* a check is any callable ``(RunResult) -> CheckResult | bool`` — built-in
  matchers, ``llm_judge``, and your own functions are all the same thing;
* ``evaluate(agent, cases)`` returns a ``Report`` you can print, assert on,
  save, and diff against yesterday's baseline.

Non-determinism is measured, not retried away: give a case ``samples=4,
pass_threshold=0.75`` and it must pass at least 3 of 4 runs.

This example runs **offline**: one shared agent definition, and every case
supplies its own scripted transcript via ``Case(model=ScriptedProvider(...))``
— the judge is scripted the same way. For a live suite, put a real model on
the agent, drop the per-case ``model=``, and drop the ``model=`` override on
``llm_judge`` (it defaults to ``$LOVIA_EVAL_JUDGE_MODEL``).

Run::

    python examples/28_eval.py
"""

from __future__ import annotations

import asyncio

from lovia import Agent, RunResult, tool
from lovia.eval import Case, Report, contains, evaluate, llm_judge, tool_called
from lovia.testing import ScriptedProvider, call, text


@tool
async def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


# One agent definition for the whole suite; each case brings its own model.
tutor = Agent(name="tutor", tools=[add])


def concise(result: RunResult) -> bool:
    """A custom check is just a function."""
    return len(str(result.output)) < 200


async def main() -> None:
    cases = [
        Case(
            "What is 2 + 3?",
            checks=[contains("5"), tool_called("add")],
            model=ScriptedProvider(
                [
                    call("add", {"a": 2, "b": 3}),
                    text("2 + 3 = 5, calculated with the tool."),
                ]
            ),
        ),
        Case(
            "What is 10 * 0?",
            checks=[contains("0"), concise],
            model=ScriptedProvider(
                [text("Anything times zero is zero — no calculator needed: 0.")]
            ),
        ),
        Case(
            "Why is 0.1 + 0.2 != 0.3 in floating point?",
            checks=[
                llm_judge(
                    "Correctly attributes the error to binary representation.",
                    # Scripted verdict keeps the demo offline; drop `model=`
                    # to grade with a real model.
                    model=ScriptedProvider(
                        [text('{"score": 0.9, "reasoning": "names binary rounding"}')]
                    ),
                )
            ],
            model=ScriptedProvider(
                [
                    text(
                        "0.1 and 0.2 have no exact binary representation, so their "
                        "sum carries a tiny rounding error: 0.30000000000000004."
                    )
                ]
            ),
        ),
    ]

    report = await evaluate(tutor, cases)
    print(report)

    # Baselines: save today's report, diff tomorrow's against it in CI.
    report.save("/tmp/lovia_eval_baseline.json")
    print(report.compare(Report.load("/tmp/lovia_eval_baseline.json")))


if __name__ == "__main__":
    asyncio.run(main())
