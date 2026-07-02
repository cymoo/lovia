"""Checks — the single evaluation concept.

A check is any callable ``(RunResult) -> CheckResult | bool``, sync or async.
Deterministic matchers, the LLM judge, and user lambdas are all checks; there
is no separate matcher/judge class tree. The factories below return
self-describing checks; a bare function works too — a ``bool`` return is
coerced into a :class:`CheckResult` named after the function::

    def cites_source(result):          # a custom check is just a function
        return "http" in str(result.output)
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence, Union

from ..runtime.result import RunResult
from ..transcript import ToolCallEntry, ToolResultEntry, to_json_safe


@dataclass
class CheckResult:
    """The outcome of one check against one run.

    ``score`` carries a continuous 0–1 grade for scored checks (judges,
    :func:`weighted`); it is ``None`` for plain pass/fail checks.
    """

    name: str
    passed: bool
    score: float | None = None
    reason: str = ""


CheckOutcome = Union[CheckResult, bool]
Check = Callable[[RunResult], "CheckOutcome | Awaitable[CheckOutcome]"]


async def run_check(check: Check, result: RunResult) -> CheckResult:
    """Evaluate one check, normalizing sync/async and ``bool`` returns.

    A check that raises fails *itself* (with the exception as reason) rather
    than aborting the surrounding evaluation.
    """
    name = getattr(check, "__name__", None) or type(check).__name__
    try:
        outcome = check(result)
        if inspect.isawaitable(outcome):
            outcome = await outcome
    except Exception as exc:
        return CheckResult(
            name=name, passed=False, reason=f"check raised {type(exc).__name__}: {exc}"
        )
    if isinstance(outcome, CheckResult):
        return outcome
    return CheckResult(name=name, passed=bool(outcome))


def _named(name: str, fn: Check) -> Check:
    """Stamp a factory-built check with a self-describing name."""
    setattr(fn, "__name__", name)
    return fn


def _snip(value: object, limit: int = 120) -> str:
    text = str(value).replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Output checks
# --------------------------------------------------------------------------- #


def contains(value: str, *, ignore_case: bool = False) -> Check:
    """Pass when ``str(output)`` contains ``value``."""

    def check(result: RunResult) -> CheckResult:
        text = str(result.output)
        found = value.lower() in text.lower() if ignore_case else value in text
        return CheckResult(
            name=f"contains({value!r})",
            passed=found,
            reason="" if found else f"not found in output: {_snip(text)}",
        )

    return _named(f"contains({value!r})", check)


def regex(pattern: str, *, flags: int = 0) -> Check:
    """Pass when ``re.search(pattern, str(output))`` matches."""
    compiled = re.compile(pattern, flags)

    def check(result: RunResult) -> CheckResult:
        text = str(result.output)
        found = compiled.search(text) is not None
        return CheckResult(
            name=f"regex({pattern!r})",
            passed=found,
            reason="" if found else f"no match in output: {_snip(text)}",
        )

    return _named(f"regex({pattern!r})", check)


def equals(value: Any) -> Check:
    """Pass when ``output == value``."""

    label = f"equals({_snip(repr(value), 40)})"

    def check(result: RunResult) -> CheckResult:
        ok = bool(result.output == value)
        return CheckResult(
            name=label,
            passed=ok,
            reason="" if ok else f"got: {_snip(repr(result.output))}",
        )

    return _named(label, check)


def matches(spec: Mapping[str, Any] | Callable[[Any], bool]) -> Check:
    """Pass when the output satisfies ``spec``.

    A mapping is matched as a recursive *subset* against the (JSON-dumped)
    structured output — extra fields in the output are ignored. A callable is
    applied to the raw output as a predicate.
    """
    if callable(spec):
        predicate = spec
        label = f"matches({getattr(spec, '__name__', 'predicate')})"

        def check(result: RunResult) -> CheckResult:
            ok = bool(predicate(result.output))
            return CheckResult(
                name=label,
                passed=ok,
                reason="" if ok else f"predicate rejected: {_snip(result.output)}",
            )

        return _named(label, check)

    expected = dict(spec)
    label = f"matches({_snip(expected, 60)})"

    def check_subset(result: RunResult) -> CheckResult:
        actual = _as_data(result.output)
        ok = _is_subset(expected, actual)
        return CheckResult(
            name=label,
            passed=ok,
            reason="" if ok else f"got: {_snip(actual)}",
        )

    return _named(label, check_subset)


def _as_data(value: Any) -> Any:
    """Normalize structured outputs (pydantic / dataclass) to plain data."""
    safe = to_json_safe(value)
    return value if safe is None and value is not None else safe


def _is_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _is_subset(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, Sequence) and not isinstance(expected, str):
        return (
            isinstance(actual, Sequence)
            and not isinstance(actual, str)
            and len(actual) == len(expected)
            and all(_is_subset(e, a) for e, a in zip(expected, actual))
        )
    return bool(expected == actual)


# --------------------------------------------------------------------------- #
# Behavior checks
# --------------------------------------------------------------------------- #


def tool_called(name: str) -> Check:
    """Pass when the run invoked the named tool at least once."""

    def check(result: RunResult) -> CheckResult:
        called = any(
            isinstance(e, ToolCallEntry) and e.name == name for e in result.entries
        )
        return CheckResult(
            name=f"tool_called({name!r})",
            passed=called,
            reason="" if called else "tool was never called",
        )

    return _named(f"tool_called({name!r})", check)


def tool_not_called(name: str) -> Check:
    """Pass when the run never invoked the named tool."""

    def check(result: RunResult) -> CheckResult:
        called = any(
            isinstance(e, ToolCallEntry) and e.name == name for e in result.entries
        )
        return CheckResult(
            name=f"tool_not_called({name!r})",
            passed=not called,
            reason="tool was called" if called else "",
        )

    return _named(f"tool_not_called({name!r})", check)


def max_turns(n: int) -> Check:
    """Pass when the run finished within ``n`` turns."""

    def check(result: RunResult) -> CheckResult:
        return CheckResult(
            name=f"max_turns({n})",
            passed=result.turns <= n,
            reason="" if result.turns <= n else f"took {result.turns} turns",
        )

    return _named(f"max_turns({n})", check)


def max_tokens(n: int) -> Check:
    """Pass when the run used at most ``n`` total tokens."""

    def check(result: RunResult) -> CheckResult:
        used = result.usage.total_tokens
        return CheckResult(
            name=f"max_tokens({n})",
            passed=used <= n,
            reason="" if used <= n else f"used {used} tokens",
        )

    return _named(f"max_tokens({n})", check)


def no_error() -> Check:
    """Pass when no tool call *within* the run failed.

    This inspects :attr:`ToolResultEntry.is_error` in a completed run. A run
    that raises (max turns, budget, provider failure) never reaches its
    checks — :func:`~lovia.eval.evaluate` records that as the sample's
    ``error`` instead.
    """

    def check(result: RunResult) -> CheckResult:
        errors = [
            e for e in result.entries if isinstance(e, ToolResultEntry) and e.is_error
        ]
        return CheckResult(
            name="no_error",
            passed=not errors,
            reason=f"{len(errors)} tool error(s): {_snip(errors[0].output)}"
            if errors
            else "",
        )

    return _named("no_error", check)


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


def all_of(*checks: Check, name: str = "all_of") -> Check:
    """Pass when every child check passes."""

    async def check(result: RunResult) -> CheckResult:
        results = [await run_check(c, result) for c in checks]
        failed = [r for r in results if not r.passed]
        return CheckResult(
            name=name,
            passed=not failed,
            reason="; ".join(f"{r.name}: {r.reason or 'failed'}" for r in failed),
        )

    return _named(name, check)


def any_of(*checks: Check, name: str = "any_of") -> Check:
    """Pass when at least one child check passes."""

    async def check(result: RunResult) -> CheckResult:
        results = [await run_check(c, result) for c in checks]
        ok = any(r.passed for r in results)
        return CheckResult(
            name=name,
            passed=ok,
            reason=""
            if ok
            else "no alternative passed: " + "; ".join(r.name for r in results),
        )

    return _named(name, check)


def weighted(
    weights: Mapping[Check, float], *, threshold: float = 0.7, name: str = "weighted"
) -> Check:
    """Combine scored checks into one weighted 0–1 score.

    Binary checks contribute 1.0/0.0; scored checks (judges) contribute their
    ``score``. Passes when the weighted average reaches ``threshold``.
    """
    if not weights or sum(weights.values()) <= 0:
        raise ValueError("weighted() needs at least one check with positive weight")

    async def check(result: RunResult) -> CheckResult:
        total = sum(weights.values())
        score = 0.0
        parts: list[str] = []
        for child, weight in weights.items():
            r = await run_check(child, result)
            s = r.score if r.score is not None else (1.0 if r.passed else 0.0)
            score += weight * s
            parts.append(f"{r.name}={s:.2f}")
        score /= total
        return CheckResult(
            name=name,
            passed=score >= threshold,
            score=score,
            reason=", ".join(parts),
        )

    return _named(name, check)


__all__ = [
    "Check",
    "CheckResult",
    "all_of",
    "any_of",
    "contains",
    "equals",
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
