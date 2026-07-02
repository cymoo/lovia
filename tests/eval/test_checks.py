"""Deterministic checks, run against hand-built RunResults."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from lovia import Agent, RunResult, Usage
from lovia.eval import (
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
from lovia.transcript import ToolCallEntry, ToolResultEntry, TranscriptEntry


def make_result(
    output: Any = "",
    entries: list[TranscriptEntry] | None = None,
    turns: int = 1,
    usage: Usage | None = None,
) -> RunResult:
    return RunResult(
        output=output,
        entries=entries or [],
        final_agent=Agent(name="t"),
        usage=usage or Usage(),
        turns=turns,
    )


# ---------- output checks ----------


async def test_contains() -> None:
    r = await run_check(contains("Paris"), make_result("Paris is the capital."))
    assert r.passed and r.name == "contains('Paris')"
    r = await run_check(contains("Paris"), make_result("London."))
    assert not r.passed
    assert "London" in r.reason


async def test_contains_ignore_case() -> None:
    check = contains("PARIS", ignore_case=True)
    assert (await run_check(check, make_result("paris!"))).passed
    assert not (await run_check(contains("PARIS"), make_result("paris!"))).passed


async def test_contains_stringifies_output() -> None:
    assert (await run_check(contains("42"), make_result(output=42))).passed


async def test_regex() -> None:
    assert (await run_check(regex(r"\b\d{4}\b"), make_result("year 2026"))).passed
    r = await run_check(regex(r"^\d+$"), make_result("abc"))
    assert not r.passed and "no match" in r.reason


async def test_regex_flags() -> None:
    check = regex("^paris", flags=re.IGNORECASE | re.MULTILINE)
    assert (await run_check(check, make_result("hello\nParis"))).passed


async def test_equals() -> None:
    assert (await run_check(equals(42), make_result(output=42))).passed
    r = await run_check(equals(42), make_result(output=43))
    assert not r.passed and "43" in r.reason


class City(BaseModel):
    name: str
    country: str
    population: int


async def test_matches_subset_on_model() -> None:
    output = City(name="Lisbon", country="PT", population=545000)
    assert (await run_check(matches({"name": "Lisbon"}), make_result(output))).passed
    assert not (await run_check(matches({"name": "Porto"}), make_result(output))).passed
    # A key absent from the output fails.
    assert not (await run_check(matches({"mayor": "x"}), make_result(output))).passed


async def test_matches_nested_and_lists() -> None:
    output = {"user": {"name": "Ada", "tags": ["a", "b"]}, "extra": 1}
    assert (
        await run_check(matches({"user": {"name": "Ada"}}), make_result(output))
    ).passed
    assert (
        await run_check(matches({"user": {"tags": ["a", "b"]}}), make_result(output))
    ).passed
    # List length must agree.
    assert not (
        await run_check(matches({"user": {"tags": ["a"]}}), make_result(output))
    ).passed


async def test_matches_mapping_against_plain_text_fails() -> None:
    assert not (await run_check(matches({"a": 1}), make_result("plain text"))).passed


async def test_matches_predicate() -> None:
    def is_short(output: Any) -> bool:
        return len(str(output)) < 10

    r = await run_check(matches(is_short), make_result("tiny"))
    assert r.passed and r.name == "matches(is_short)"
    assert not (await run_check(matches(is_short), make_result("x" * 20))).passed


# ---------- behavior checks ----------


def tool_entries() -> list[TranscriptEntry]:
    return [
        ToolCallEntry(call_id="1", name="search", arguments="{}"),
        ToolResultEntry(call_id="1", output="ok"),
    ]


async def test_tool_called() -> None:
    assert (
        await run_check(tool_called("search"), make_result(entries=tool_entries()))
    ).passed
    r = await run_check(tool_called("fetch"), make_result(entries=tool_entries()))
    assert not r.passed and r.reason == "tool was never called"


async def test_tool_not_called() -> None:
    assert (
        await run_check(tool_not_called("fetch"), make_result(entries=tool_entries()))
    ).passed
    assert not (
        await run_check(tool_not_called("search"), make_result(entries=tool_entries()))
    ).passed


async def test_max_turns_boundary() -> None:
    assert (await run_check(max_turns(3), make_result(turns=3))).passed
    r = await run_check(max_turns(3), make_result(turns=4))
    assert not r.passed and "4 turns" in r.reason


async def test_max_tokens_boundary() -> None:
    usage = Usage(input_tokens=60, output_tokens=40)
    assert (await run_check(max_tokens(100), make_result(usage=usage))).passed
    assert not (await run_check(max_tokens(99), make_result(usage=usage))).passed


async def test_no_error() -> None:
    assert (await run_check(no_error(), make_result(entries=tool_entries()))).passed
    bad: list[TranscriptEntry] = [
        ToolResultEntry(call_id="1", output="boom", is_error=True)
    ]
    r = await run_check(no_error(), make_result(entries=bad))
    assert not r.passed and "boom" in r.reason


# ---------- run_check normalization ----------


async def test_bool_function_is_a_check() -> None:
    def cites_source(result: RunResult) -> bool:
        return "http" in str(result.output)

    r = await run_check(cites_source, make_result("see https://x.dev"))
    assert r.passed and r.name == "cites_source"


async def test_async_check() -> None:
    async def slow_check(result: RunResult) -> bool:
        return True

    assert (await run_check(slow_check, make_result())).passed


async def test_check_returning_checkresult_passthrough() -> None:
    def scored(result: RunResult) -> CheckResult:
        return CheckResult(name="scored", passed=True, score=0.5)

    r = await run_check(scored, make_result())
    assert r.score == 0.5


async def test_raising_check_fails_itself() -> None:
    def broken(result: RunResult) -> bool:
        raise KeyError("oops")

    r = await run_check(broken, make_result())
    assert not r.passed
    assert r.name == "broken"
    assert "KeyError" in r.reason


# ---------- composition ----------


async def test_all_of() -> None:
    result = make_result("Paris, 2026")
    assert (await run_check(all_of(contains("Paris"), regex(r"\d{4}")), result)).passed
    r = await run_check(all_of(contains("Paris"), contains("London")), result)
    assert not r.passed
    assert "contains('London')" in r.reason


async def test_all_of_empty_is_vacuously_true() -> None:
    assert (await run_check(all_of(), make_result())).passed


async def test_any_of() -> None:
    result = make_result("Paris")
    assert (
        await run_check(any_of(contains("London"), contains("Paris")), result)
    ).passed
    r = await run_check(any_of(contains("London"), contains("Rome")), result)
    assert not r.passed and "no alternative passed" in r.reason


async def test_weighted() -> None:
    def half(result: RunResult) -> CheckResult:
        return CheckResult(name="half", passed=True, score=0.5)

    check = weighted({contains("Paris"): 1.0, half: 1.0}, threshold=0.7)
    r = await run_check(check, make_result("Paris"))
    # (1.0 + 0.5) / 2 = 0.75 >= 0.7
    assert r.passed and r.score == 0.75
    strict = weighted({contains("Paris"): 1.0, half: 1.0}, threshold=0.8)
    assert not (await run_check(strict, make_result("Paris"))).passed


async def test_weighted_buggy_child_contributes_zero() -> None:
    def broken(result: RunResult) -> bool:
        raise RuntimeError("boom")

    check = weighted({contains("x"): 1.0, broken: 1.0}, threshold=0.4)
    r = await run_check(check, make_result("x"))
    assert r.score == 0.5 and r.passed


def test_weighted_rejects_empty_or_zero_weights() -> None:
    import pytest

    with pytest.raises(ValueError):
        weighted({})
    with pytest.raises(ValueError):
        weighted({contains("x"): 0.0})


async def test_composition_custom_name() -> None:
    r = await run_check(all_of(contains("x"), name="sanity"), make_result("x"))
    assert r.name == "sanity"
