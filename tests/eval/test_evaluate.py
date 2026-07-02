"""The evaluate() engine, driven by scripted agents."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from lovia import Agent, Usage, tool, user
from lovia.eval import Case, contains, equals, evaluate, matches, tool_called
from lovia.testing import ScriptedProvider, call, text


def echo_agent(*replies: str) -> Agent[None]:
    return Agent(name="echo", model=ScriptedProvider([text(r) for r in replies]))


# ---------- Case ----------


def test_case_name_derived_from_input() -> None:
    assert Case("What is 2+2?").name == "What is 2+2?"
    long = Case("word " * 30)
    assert len(long.name) == 48 and long.name.endswith("…")
    assert Case("a\n b\tc").name == "a b c"


def test_case_name_derived_from_messages() -> None:
    assert Case([user("hi there")]).name == "hi there"
    assert Case([]).name == "case"


def test_case_explicit_name_wins() -> None:
    assert Case("input", name="my-case").name == "my-case"


def test_case_validation() -> None:
    with pytest.raises(ValueError):
        Case("x", samples=0)
    with pytest.raises(ValueError):
        Case("x", pass_threshold=1.5)


# ---------- evaluate ----------


async def test_single_passing_case() -> None:
    report = await evaluate(
        lambda: echo_agent("Paris is the capital of France."),
        Case("Capital of France?", checks=[contains("Paris")]),
    )
    assert report.passed
    assert report.cases[0].name == "Capital of France?"
    sample = report.cases[0].samples[0]
    assert sample.passed
    assert sample.output == "Paris is the capital of France."
    assert sample.turns == 1
    assert sample.usage.total_tokens > 0
    assert sample.latency > 0
    assert sample.error is None


async def test_failing_check_fails_case_and_report() -> None:
    report = await evaluate(
        lambda: echo_agent("London."),
        [Case("q", checks=[contains("Paris")])],
    )
    assert not report.passed
    assert report.cases[0].samples[0].checks[0].reason != ""


async def test_sampling_and_pass_threshold() -> None:
    # Scripted replies differ per sample: 2 of 3 contain "Paris".
    replies = iter(["Paris", "Paris", "Lyon"])

    factory = lambda: echo_agent(next(replies))  # noqa: E731

    passing = await evaluate(
        factory,
        Case("q", checks=[contains("Paris")], samples=3, pass_threshold=0.6),
    )
    assert passing.passed
    assert passing.cases[0].pass_rate == pytest.approx(2 / 3)

    replies = iter(["Paris", "Paris", "Lyon"])
    strict = await evaluate(factory, Case("q", checks=[contains("Paris")], samples=3))
    assert not strict.passed


async def test_run_error_is_sample_data_not_suite_abort() -> None:
    # An empty script raises inside the first run; the second case still runs.
    scripts = iter([[], [text("hi")]])
    report = await evaluate(
        lambda: Agent(name="e", model=ScriptedProvider(next(scripts))),
        [Case("boom", checks=[contains("x")]), Case("ok", checks=[contains("hi")])],
        concurrency=1,
    )
    broken, fine = report.cases
    assert not report.passed
    assert broken.samples[0].error is not None
    assert "AssertionError" in broken.samples[0].error
    assert broken.samples[0].checks == []  # checks never ran
    assert fine.passed


async def test_tool_and_structured_output_cases() -> None:
    @tool
    async def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    class Answer(BaseModel):
        value: int

    def calc_factory() -> Agent[None]:
        return Agent(
            name="calc",
            model=ScriptedProvider(
                [call("add", {"a": 2, "b": 3}), text('{"value": 5}')]
            ),
            tools=[add],
        )

    report = await evaluate(
        calc_factory,
        Case(
            "2+3?",
            checks=[tool_called("add"), matches({"value": 5}), equals(Answer(value=5))],
            output_type=Answer,
        ),
    )
    assert report.passed, str(report)


async def test_fail_fast_stops_after_first_failure() -> None:
    ran: list[str] = []

    def factory() -> Agent[None]:
        ran.append("run")
        return echo_agent("nope")

    cases = [
        Case("a", checks=[contains("nope")]),
        Case("b", checks=[contains("yes")]),  # fails
        Case("c", checks=[contains("nope")]),  # never runs
    ]
    report = await evaluate(factory, cases, fail_fast=True)
    assert [c.name for c in report.cases] == ["a", "b"]
    assert len(ran) == 2


async def test_concurrency_and_order_preserved() -> None:
    cases = [Case(f"q{i}", checks=[contains("hi")]) for i in range(10)]
    report = await evaluate(lambda: echo_agent("hi"), cases, concurrency=5)
    assert [c.name for c in report.cases] == [f"q{i}" for i in range(10)]
    assert report.passed


async def test_price_callback_and_robustness() -> None:
    def price(usage: Usage) -> float:
        return usage.total_tokens * 0.5

    report = await evaluate(
        lambda: echo_agent("hi"), Case("q", checks=[contains("hi")]), price=price
    )
    assert report.cases[0].samples[0].cost == pytest.approx(1.0)

    def broken_price(usage: Usage) -> float:
        raise RuntimeError("no table")

    report = await evaluate(
        lambda: echo_agent("hi"), Case("q", checks=[contains("hi")]), price=broken_price
    )
    assert report.cases[0].samples[0].cost is None
    assert report.passed  # a broken price never fails the eval


async def test_timeout_recorded_as_error() -> None:
    @tool
    async def slow() -> str:
        """Sleep."""
        await asyncio.sleep(5)
        return "done"

    def factory() -> Agent[None]:
        return Agent(
            name="s",
            model=ScriptedProvider([call("slow", {}), text("done")]),
            tools=[slow],
        )

    report = await evaluate(factory, Case("q", checks=[contains("done")], timeout=0.05))
    sample = report.cases[0].samples[0]
    assert sample.error is not None and "timeout" in sample.error
    assert not report.passed


async def test_plain_agent_instance_accepted() -> None:
    report = await evaluate(echo_agent("hello"), Case("q", checks=[contains("hello")]))
    assert report.passed


async def test_max_turns_forwarded() -> None:
    @tool
    async def ping() -> str:
        """Ping."""
        return "pong"

    def factory() -> Agent[None]:
        return Agent(
            name="loop",
            model=ScriptedProvider([call("ping", {}), call("ping", {}), text("hi")]),
            tools=[ping],
        )

    report = await evaluate(factory, Case("q", max_turns=1, checks=[contains("hi")]))
    sample = report.cases[0].samples[0]
    assert sample.error is not None and "MaxTurnsExceeded" in sample.error
