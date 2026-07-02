"""Report aggregation, serialization round-trip, baseline diff, rendering."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from lovia import Usage
from lovia.eval import CaseResult, CheckResult, Report, SampleResult


def sample(passed: bool = True, **kwargs: object) -> SampleResult:
    checks = [CheckResult(name="c", passed=passed, reason="" if passed else "nope")]
    return SampleResult(checks=checks, **kwargs)  # type: ignore[arg-type]


def case(name: str, *passes: bool, threshold: float = 1.0) -> CaseResult:
    return CaseResult(
        name=name, samples=[sample(p) for p in passes], pass_threshold=threshold
    )


# ---------- aggregation ----------


def test_sample_passed_logic() -> None:
    assert sample(True).passed
    assert not sample(False).passed
    assert not SampleResult(error="boom").passed
    assert SampleResult().passed  # no checks, no error


def test_case_pass_rate_and_threshold() -> None:
    c = case("x", True, True, False, threshold=0.6)
    assert c.pass_rate == pytest.approx(2 / 3)
    assert c.passed
    assert not case("x", True, False).passed  # default threshold 1.0
    assert not CaseResult(name="empty", samples=[]).passed


def test_pass_at_k() -> None:
    c = case("x", True, False, False, False)
    assert c.pass_at_k(1) == pytest.approx(0.25)
    # 1 - C(3,2)/C(4,2) = 1 - 3/6
    assert c.pass_at_k(2) == pytest.approx(0.5)
    assert c.pass_at_k(4) == 1.0
    with pytest.raises(ValueError):
        c.pass_at_k(5)
    with pytest.raises(ValueError):
        c.pass_at_k(0)


def test_report_aggregates() -> None:
    report = Report(cases=[case("a", True), case("b", False)])
    assert not report.passed
    assert report.pass_rate == 0.5
    assert Report(cases=[]).passed
    assert Report(cases=[]).pass_rate == 1.0


# ---------- serialization ----------


class Out(BaseModel):
    n: int


def test_round_trip(tmp_path: Path) -> None:
    report = Report(
        cases=[
            CaseResult(
                name="structured",
                pass_threshold=0.5,
                metadata={"suite": "smoke"},
                samples=[
                    SampleResult(
                        checks=[CheckResult(name="c", passed=True, score=0.9)],
                        output=Out(n=7),
                        turns=2,
                        usage=Usage(input_tokens=10, output_tokens=5),
                        latency=1.5,
                        cost=0.01,
                    ),
                    SampleResult(error="ProviderError: 500"),
                ],
            )
        ]
    )
    path = tmp_path / "baseline.json"
    report.save(path)
    loaded = Report.load(path)

    assert loaded.passed == report.passed
    lcase = loaded.cases[0]
    assert lcase.name == "structured"
    assert lcase.pass_threshold == 0.5
    assert lcase.metadata == {"suite": "smoke"}
    assert lcase.pass_rate == report.cases[0].pass_rate
    ok, bad = lcase.samples
    assert ok.output == {"n": 7}  # models serialize to plain data
    assert ok.usage.total_tokens == 15
    assert ok.checks[0].score == 0.9
    assert bad.error == "ProviderError: 500"
    assert not bad.passed


def test_from_dict_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        Report.from_dict({"schema_version": 99, "cases": []})
    # A missing version is treated as current (hand-rolled dicts stay easy).
    assert Report.from_dict({"cases": []}).cases == []


def test_unserializable_output_falls_back_to_repr() -> None:
    report = Report(
        cases=[CaseResult(name="x", samples=[SampleResult(output=object())])]
    )
    dumped = report.to_dict()
    assert "object object" in dumped["cases"][0]["samples"][0]["output"]


# ---------- baseline diff ----------


def test_compare() -> None:
    baseline = Report(cases=[case("a", True), case("b", False), case("gone", True)])
    current = Report(cases=[case("a", False), case("b", True), case("new", True)])
    diff = current.compare(baseline)
    assert diff.regressions == ["a"]
    assert diff.improvements == ["b"]
    assert diff.added == ["new"]
    assert diff.removed == ["gone"]
    assert not diff.ok
    assert not diff  # __bool__ mirrors ok
    assert "regressed: a" in str(diff)


def test_compare_rejects_duplicate_case_names() -> None:
    dup = Report(cases=[case("a", True), case("a", False)])
    clean = Report(cases=[case("a", True)])
    with pytest.raises(ValueError, match="duplicate case names in current"):
        dup.compare(clean)
    with pytest.raises(ValueError, match="duplicate case names in baseline"):
        clean.compare(dup)


def test_compare_clean() -> None:
    report = Report(cases=[case("a", True)])
    diff = report.compare(report)
    assert diff.ok and bool(diff)
    assert str(diff) == "baseline: no changes"


# ---------- rendering ----------


def test_report_str() -> None:
    report = Report(
        cases=[
            case("passing-case", True, True),
            CaseResult(
                name="failing-case",
                samples=[
                    SampleResult(
                        checks=[
                            CheckResult(
                                name="llm_judge",
                                passed=False,
                                score=0.4,
                                reason="too terse",
                            )
                        ]
                    )
                ],
            ),
        ]
    )
    rendered = str(report)
    assert "1/2 cases passed (50%)" in rendered
    assert "✓ passing-case" in rendered
    assert "✗ failing-case" in rendered
    assert "llm_judge (score 0.40) — too terse" in rendered


def test_report_str_shows_error_and_cost() -> None:
    report = Report(
        cases=[
            CaseResult(name="err", samples=[SampleResult(error="boom", cost=0.5)]),
        ]
    )
    rendered = str(report)
    assert "error: boom" in rendered
    assert "$0.5000" in rendered
