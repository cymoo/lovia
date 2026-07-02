"""lovia.eval — declarative evaluation for agents.

Three ideas cover everything:

* a **Case** pairs an input with the checks its run must satisfy;
* a **check** is any callable ``(RunResult) -> CheckResult | bool`` — the
  built-in matchers, :func:`llm_judge`, and your own functions are all the
  same thing;
* :func:`evaluate` runs the cases and returns a :class:`Report` you can
  print, assert on, save, and diff against a baseline.

Typical use::

    from lovia.eval import Case, contains, evaluate, llm_judge, tool_called

    cases = [
        Case("What is the capital of France?", checks=[contains("Paris")]),
        Case(
            "Write a haiku about spring",
            checks=[llm_judge("A 5-7-5 haiku that evokes spring")],
            samples=4,
            pass_threshold=0.75,
        ),
        Case("What's 23.4 * 91?", checks=[tool_called("calculator")]),
    ]

    report = await evaluate(agent, cases)
    print(report)
    assert report.passed

Non-determinism is measured, not retried away: ``samples`` reruns a case and
``pass_threshold`` sets the pass rate it must reach. For offline suites, pair
:func:`evaluate` with a :class:`lovia.testing.ScriptedProvider` agent factory.
"""

from .checks import (
    Check,
    CheckResult,
    all_of,
    any_of,
    contains,
    equals,
    matches,
    max_tokens,
    max_turns,
    no_error,
    regex,
    run_check,
    tool_called,
    tool_not_called,
    weighted,
)
from .report import CaseResult, Diff, Report, SampleResult
from .runner import AgentSource, Case, evaluate

__all__ = [
    "AgentSource",
    "Case",
    "CaseResult",
    "Check",
    "CheckResult",
    "Diff",
    "Report",
    "SampleResult",
    "all_of",
    "any_of",
    "contains",
    "equals",
    "evaluate",
    "matches",
    "max_tokens",
    "max_turns",
    "no_error",
    "regex",
    "run_check",
    "tool_called",
    "tool_not_called",
    "weighted",
]
