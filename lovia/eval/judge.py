"""LLM-as-judge: semantic grading as just another check.

The judge is a plain lovia agent with ``output_type=Verdict`` — structured
output parsing, repair, and provider retries are all inherited from the
runtime rather than reimplemented here.
"""

from __future__ import annotations

import os
from typing import cast

from pydantic import BaseModel, Field

from ..agent import Agent
from ..exceptions import UserError
from ..parts import text_of
from ..providers import ModelSettings, Provider
from ..runner import Runner
from ..runtime.result import RunResult
from ..transcript import InputEntry
from .checks import Check, CheckResult, _named


class Verdict(BaseModel):
    """A judge's structured ruling."""

    score: float = Field(ge=0.0, le=1.0)
    reasoning: str


_INSTRUCTIONS = """\
You are an impartial evaluator. You will be shown a rubric, the input given
to an AI assistant, and the output it produced. Judge ONLY whether the output
satisfies the rubric — ignore style preferences the rubric does not mention.

Score from 0.0 (completely fails the rubric) to 1.0 (fully satisfies it),
using the full range for partial fulfillment. Keep the reasoning to one or
two sentences."""


def llm_judge(
    rubric: str,
    *,
    model: str | Provider | None = None,
    threshold: float = 0.7,
    name: str = "llm_judge",
) -> Check:
    """A check that grades the output against ``rubric`` with an LLM.

    Passes when the judge's score reaches ``threshold``. ``model`` accepts a
    ``"vendor:model"`` string or a :class:`~lovia.Provider` instance (a
    scripted provider makes the judge deterministic in tests); when omitted,
    ``$LOVIA_EVAL_JUDGE_MODEL`` is used, and if neither is set a
    :class:`UserError` is raised — grading quality depends on the judge
    model, so lovia never picks one silently. Each evaluation is one model
    call — judge cost scales with ``samples`` × number of judge checks.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("llm_judge threshold must be within 0..1")
    resolved = model if model is not None else os.getenv("LOVIA_EVAL_JUDGE_MODEL")
    if not resolved:
        raise UserError(
            "llm_judge has no model configured",
            hint='pass llm_judge(..., model="vendor:model") '
            "or set LOVIA_EVAL_JUDGE_MODEL",
        )
    judge: Agent[None] = Agent(
        name="lovia-eval-judge",
        instructions=_INSTRUCTIONS,
        model=resolved,
        output_type=Verdict,
        settings=ModelSettings(temperature=0.0),
    )

    async def check(result: RunResult) -> CheckResult:
        prompt = (
            f"<rubric>\n{rubric}\n</rubric>\n\n"
            f"<input>\n{_input_text(result)}\n</input>\n\n"
            f"<output>\n{result.output}\n</output>"
        )
        verdict = cast(Verdict, (await Runner.run(judge, prompt)).output)
        return CheckResult(
            name=name,
            passed=verdict.score >= threshold,
            score=verdict.score,
            reason=verdict.reasoning,
        )

    return _named(name, check)


def _input_text(result: RunResult) -> str:
    """Recover the user-visible input from the run's own transcript."""
    texts = [
        text_of(e.content)
        for e in result.entries
        if isinstance(e, InputEntry) and e.role == "user"
    ]
    return "\n\n".join(t for t in texts if t)


__all__ = ["Verdict", "llm_judge"]
