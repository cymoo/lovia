"""Eval results: per-sample, per-case, and suite-level.

Plain dataclasses all the way down — a custom reporter is any function that
consumes a :class:`Report`. ``Report.save`` / ``Report.load`` round-trip
through JSON so a checked-in baseline can be diffed with
:meth:`Report.compare` to catch regressions in CI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from math import comb
from pathlib import Path
from typing import Any

from ..messages import Usage
from ..transcript import to_json_safe
from .checks import CheckResult

_SCHEMA_VERSION = 1


@dataclass
class SampleResult:
    """One run of a case: its check outcomes, or the error that ended it."""

    checks: list[CheckResult] = field(default_factory=list)
    output: Any = None
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    latency: float = 0.0  # wall-clock seconds
    cost: float | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)


@dataclass
class CaseResult:
    """All samples of one case, aggregated against its pass threshold."""

    name: str
    samples: list[SampleResult]
    pass_threshold: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        if not self.samples:
            return 0.0
        return sum(1 for s in self.samples if s.passed) / len(self.samples)

    @property
    def passed(self) -> bool:
        return bool(self.samples) and self.pass_rate >= self.pass_threshold

    def pass_at_k(self, k: int) -> float:
        """Unbiased pass@k estimate: P(at least one of k draws passes)."""
        n = len(self.samples)
        c = sum(1 for s in self.samples if s.passed)
        if not 1 <= k <= n:
            raise ValueError(f"k must be in 1..{n}, got {k}")
        if n - c < k:
            return 1.0
        return 1.0 - comb(n - c, k) / comb(n, k)

    def _failure(self) -> str:
        """A one-line reason from the first failed sample, for display."""
        for sample in self.samples:
            if sample.passed:
                continue
            if sample.error is not None:
                return f"error: {sample.error}"
            for check in sample.checks:
                if not check.passed:
                    score = (
                        f" (score {check.score:.2f})" if check.score is not None else ""
                    )
                    reason = f" — {check.reason}" if check.reason else ""
                    return f"{check.name}{score}{reason}"
        return ""


@dataclass
class Report:
    """The outcome of one :func:`~lovia.eval.evaluate` call."""

    cases: list[CaseResult]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    @property
    def pass_rate(self) -> float:
        if not self.cases:
            return 1.0
        return sum(1 for c in self.cases if c.passed) / len(self.cases)

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "cases": [
                {
                    "name": case.name,
                    "passed": case.passed,
                    "pass_rate": case.pass_rate,
                    "pass_threshold": case.pass_threshold,
                    "metadata": case.metadata,
                    "samples": [_sample_to_dict(s) for s in case.samples],
                }
                for case in self.cases
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Report":
        version = data.get("schema_version", _SCHEMA_VERSION)
        if version != _SCHEMA_VERSION:
            raise ValueError(
                f"unsupported report schema_version {version!r}; "
                f"this lovia reads version {_SCHEMA_VERSION}"
            )
        return cls(
            cases=[
                CaseResult(
                    name=case["name"],
                    pass_threshold=case.get("pass_threshold", 1.0),
                    metadata=case.get("metadata", {}),
                    samples=[_sample_from_dict(s) for s in case.get("samples", [])],
                )
                for case in data.get("cases", [])
            ]
        )

    def save(self, path: str | Path) -> None:
        """Write the report as JSON — e.g. a baseline checked into the repo."""
        payload = json.dumps(self.to_dict(), indent=2) + "\n"
        Path(path).write_text(payload, encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Report":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ #
    # Baseline diff
    # ------------------------------------------------------------------ #

    def compare(self, baseline: "Report") -> "Diff":
        """Diff this report against a ``baseline`` (by case name).

        Raises :class:`ValueError` when either report contains duplicate case
        names — the diff would silently drop all but one of them.
        """
        for label, report in (("current", self), ("baseline", baseline)):
            names = [case.name for case in report.cases]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(f"duplicate case names in {label} report: {dupes}")
        ours = {case.name: case for case in self.cases}
        theirs = {case.name: case for case in baseline.cases}
        return Diff(
            regressions=[
                name
                for name, case in ours.items()
                if name in theirs and theirs[name].passed and not case.passed
            ],
            improvements=[
                name
                for name, case in ours.items()
                if name in theirs and not theirs[name].passed and case.passed
            ],
            added=[name for name in ours if name not in theirs],
            removed=[name for name in theirs if name not in ours],
        )

    # ------------------------------------------------------------------ #
    # Display
    # ------------------------------------------------------------------ #

    def __str__(self) -> str:
        n_samples = sum(len(case.samples) for case in self.cases)
        tokens = sum(s.usage.total_tokens for case in self.cases for s in case.samples)
        latency = sum(s.latency for case in self.cases for s in case.samples)
        costs = [
            s.cost for case in self.cases for s in case.samples if s.cost is not None
        ]
        n_passed = sum(1 for case in self.cases if case.passed)
        header = (
            f"eval: {n_passed}/{len(self.cases)} cases passed "
            f"({self.pass_rate:.0%}) · {n_samples} samples "
            f"· {tokens:,} tokens · {latency:.1f}s"
        )
        if costs:
            header += f" · ${sum(costs):.4f}"
        width = max((len(case.name) for case in self.cases), default=0)
        lines = [header]
        for case in self.cases:
            mark = "✓" if case.passed else "✗"
            counts = f"{sum(1 for s in case.samples if s.passed)}/{len(case.samples)}"
            line = f"  {mark} {case.name:<{width}}  {counts}"
            if not case.passed:
                line += f"  {case._failure()}"
            lines.append(line)
        return "\n".join(lines)


@dataclass
class Diff:
    """Case-level differences between two reports. Truthy when clean."""

    regressions: list[str]
    improvements: list[str]
    added: list[str]
    removed: list[str]

    @property
    def ok(self) -> bool:
        """True when nothing that used to pass now fails."""
        return not self.regressions

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        if self.ok and not any((self.improvements, self.added, self.removed)):
            return "baseline: no changes"
        lines = ["baseline: REGRESSIONS" if not self.ok else "baseline: ok"]
        for label, names in (
            ("regressed", self.regressions),
            ("improved", self.improvements),
            ("added", self.added),
            ("removed", self.removed),
        ):
            if names:
                lines.append(f"  {label}: {', '.join(sorted(names))}")
        return "\n".join(lines)


def _sample_to_dict(sample: SampleResult) -> dict[str, Any]:
    output = to_json_safe(sample.output)
    if output is None and sample.output is not None:
        output = repr(sample.output)
    return {
        "passed": sample.passed,
        "output": output,
        "turns": sample.turns,
        "usage": {
            "input_tokens": sample.usage.input_tokens,
            "output_tokens": sample.usage.output_tokens,
            "cache_read_tokens": sample.usage.cache_read_tokens,
            "cache_write_tokens": sample.usage.cache_write_tokens,
        },
        "latency": sample.latency,
        "cost": sample.cost,
        "error": sample.error,
        "checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "score": c.score,
                "reason": c.reason,
            }
            for c in sample.checks
        ],
    }


def _sample_from_dict(data: dict[str, Any]) -> SampleResult:
    return SampleResult(
        checks=[
            CheckResult(
                name=c["name"],
                passed=c["passed"],
                score=c.get("score"),
                reason=c.get("reason", ""),
            )
            for c in data.get("checks", [])
        ],
        output=data.get("output"),
        turns=data.get("turns", 0),
        usage=Usage(**data.get("usage", {})),
        latency=data.get("latency", 0.0),
        cost=data.get("cost"),
        error=data.get("error"),
    )


__all__ = ["CaseResult", "Diff", "Report", "SampleResult"]
