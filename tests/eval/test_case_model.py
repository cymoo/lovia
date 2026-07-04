"""``Case.model`` — per-case model/provider override in the eval runner."""

from __future__ import annotations

from lovia import Agent, tool
from lovia.eval import Case, contains, evaluate, tool_called
from lovia.testing import ScriptedProvider, call, text


@tool
async def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


async def test_each_case_runs_on_its_own_provider() -> None:
    # One shared agent definition, no model of its own; every case supplies
    # a scripted provider — the pattern for offline suites.
    tutor = Agent(name="tutor", tools=[add])

    cases = [
        Case(
            "What is 2 + 3?",
            checks=[contains("5"), tool_called("add")],
            model=ScriptedProvider([call("add", {"a": 2, "b": 3}), text("2 + 3 = 5.")]),
        ),
        Case(
            "What is 10 * 0?",
            checks=[contains("0")],
            model=ScriptedProvider([text("Zero: 0.")]),
        ),
    ]

    report = await evaluate(tutor, cases)
    assert report.passed
    assert [c.passed for c in report.cases] == [True, True]


async def test_case_model_overrides_agent_model() -> None:
    agent = Agent(name="a", model=ScriptedProvider([text("from agent")]))
    case = Case(
        "hi",
        checks=[contains("from case")],
        model=ScriptedProvider([text("from case")]),
    )
    report = await evaluate(agent, [case])
    assert report.passed


async def test_case_without_model_uses_agent_model() -> None:
    agent = Agent(name="a", model=ScriptedProvider([text("agent answer")]))
    report = await evaluate(agent, [Case("hi", checks=[contains("agent answer")])])
    assert report.passed
