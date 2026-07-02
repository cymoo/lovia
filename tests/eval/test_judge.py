"""llm_judge with a scripted judge model — deterministic, offline."""

from __future__ import annotations

import json

import pytest

from lovia import Agent, RunResult, Usage
from lovia.eval import Case, evaluate, llm_judge, run_check
from lovia.testing import ScriptedProvider, text
from lovia.transcript import InputEntry


def verdict(score: float, reasoning: str = "because") -> str:
    return json.dumps({"score": score, "reasoning": reasoning})


def make_result(output: str, question: str = "What is the capital?") -> RunResult:
    return RunResult(
        output=output,
        entries=[InputEntry(role="user", content=question)],
        final_agent=Agent(name="t"),
        usage=Usage(),
        turns=1,
    )


async def test_judge_passes_at_threshold() -> None:
    judge = llm_judge(
        "Answer names the correct capital.",
        model=ScriptedProvider([text(verdict(0.9, "names Paris"))]),
        threshold=0.9,
    )
    r = await run_check(judge, make_result("Paris."))
    assert r.passed
    assert r.score == 0.9
    assert r.reason == "names Paris"
    assert r.name == "llm_judge"


async def test_judge_fails_below_threshold() -> None:
    judge = llm_judge(
        "Answer names the correct capital.",
        model=ScriptedProvider([text(verdict(0.3, "wrong city"))]),
    )
    r = await run_check(judge, make_result("London."))
    assert not r.passed and r.score == 0.3


async def test_judge_prompt_carries_rubric_input_and_output() -> None:
    provider = ScriptedProvider([text(verdict(1.0))])
    judge = llm_judge("Mentions the Seine river.", model=provider)
    await run_check(judge, make_result("The Seine flows through Paris."))
    prompt = provider.calls[0][-1].text
    assert "Mentions the Seine river." in prompt
    assert "What is the capital?" in prompt
    assert "The Seine flows through Paris." in prompt


async def test_judge_input_joins_all_user_messages() -> None:
    provider = ScriptedProvider([text(verdict(1.0))])
    judge = llm_judge("rubric", model=provider)
    result = RunResult(
        output="fine",
        entries=[
            InputEntry(role="system", content="be brief"),
            InputEntry(role="user", content="first question"),
            InputEntry(role="user", content="second question"),
        ],
        final_agent=Agent(name="t"),
        usage=Usage(),
        turns=1,
    )
    await run_check(judge, result)
    prompt = provider.calls[0][-1].text
    assert "first question" in prompt and "second question" in prompt
    assert "be brief" not in prompt  # system prompt is not the user input


async def test_judge_output_repair_kicks_in() -> None:
    # First judge reply is not valid JSON; the runtime repairs it for free.
    provider = ScriptedProvider([text("hmm, let me think"), text(verdict(0.8))])
    judge = llm_judge("rubric", model=provider, threshold=0.7)
    r = await run_check(judge, make_result("x"))
    assert r.passed and r.score == 0.8


async def test_judge_error_fails_the_check_only() -> None:
    # An exhausted script raises inside the judge; run_check contains it.
    judge = llm_judge("rubric", model=ScriptedProvider([]), name="quality")
    r = await run_check(judge, make_result("x"))
    assert not r.passed
    assert r.name == "quality"
    assert "check raised" in r.reason


async def test_judge_inside_evaluate() -> None:
    def factory() -> Agent[None]:
        return Agent(name="poet", model=ScriptedProvider([text("An ode to spring")]))

    judge = llm_judge(
        "A poem about spring.",
        model=ScriptedProvider([text(verdict(1.0, "clearly spring"))]),
    )
    report = await evaluate(factory, Case("write a poem", checks=[judge]))
    assert report.passed
    assert report.cases[0].samples[0].checks[0].score == 1.0


def test_judge_threshold_validation() -> None:
    with pytest.raises(ValueError):
        llm_judge("rubric", threshold=1.2)


def test_judge_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_EVAL_JUDGE_MODEL", "anthropic:claude-fable-5")
    # Factory-time resolution: no provider call is made, so this stays offline.
    check = llm_judge("rubric")
    assert check.__name__ == "llm_judge"  # type: ignore[attr-defined]
