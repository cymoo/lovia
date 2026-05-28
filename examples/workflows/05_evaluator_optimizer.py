"""Evaluator-Optimizer workflow.

One LLM generates a response while a second acts as an evaluator, providing
structured feedback. The loop continues until the evaluator is satisfied or
the maximum number of iterations is reached.

This pattern is analogous to a human editor reviewing drafts of a document.

Demo: Iteratively refine a literary translation (English → French) until
the evaluator deems it high quality.

Two signs this pattern is a good fit:
  1. LLM output can be demonstrably improved when given explicit feedback.
  2. An LLM can reliably provide that feedback.

Reference:
  https://www.anthropic.com/engineering/building-effective-agents#evaluator-optimizer

Run::

    python examples/workflows/05_evaluator_optimizer.py
"""

from __future__ import annotations

import asyncio
import os
from typing import Literal

from pydantic import BaseModel

from lovia import Agent, Runner
from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")

MAX_ITERATIONS = 4

# ---------------------------------------------------------------------------
# Source text (English excerpt — public domain)
# ---------------------------------------------------------------------------

SOURCE_TEXT = """\
It was the best of times, it was the worst of times, it was the age of wisdom,
it was the age of foolishness, it was the epoch of belief, it was the epoch of
incredulity, it was the season of Light, it was the season of Darkness, it was
the spring of hope, it was the winter of despair.
"""


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EvaluationResult(BaseModel):
    score: Literal["poor", "acceptable", "good", "excellent"]
    issues: list[str]
    suggestions: list[str]
    approved: bool


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

translator_agent = Agent(
    name="Translator",
    instructions=(
        "You are an expert literary translator specializing in English-to-French "
        "translation. Produce translations that are faithful to the meaning while "
        "preserving the rhetorical style and rhythm of the original. "
        "If feedback is provided, apply it carefully in your revised translation. "
        "Return the translation only, with no commentary."
    ),
    model=MODEL,
)

evaluator_agent = Agent(
    name="TranslationEvaluator",
    instructions=(
        "You are a senior French literary editor and translator. Evaluate the "
        "provided French translation of an English source text.\n\n"
        "Assess:\n"
        "  • Fidelity to the source meaning\n"
        "  • Preservation of rhetorical style and rhythm\n"
        "  • Naturalness of the French prose\n"
        "  • Any awkward phrasings or word-choice issues\n\n"
        "Set 'approved' to true only when the translation is 'good' or 'excellent' "
        "AND has no significant issues remaining."
    ),
    model=MODEL,
    output_type=EvaluationResult,
)


# ---------------------------------------------------------------------------
# Optimization loop
# ---------------------------------------------------------------------------


async def optimize_translation(source: str) -> str:
    feedback_context = ""
    current_translation = ""

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n[Iteration {iteration}] Generating translation …")

        if feedback_context:
            translator_prompt = (
                f"Source text:\n{source}\n\n"
                f"Previous translation:\n{current_translation}\n\n"
                f"Evaluator feedback:\n{feedback_context}\n\n"
                "Produce an improved translation addressing the feedback."
            )
        else:
            translator_prompt = f"Translate this text to French:\n\n{source}"

        translation_result = await Runner.run(translator_agent, translator_prompt)
        current_translation = translation_result.output
        print(f"Translation:\n{current_translation}")

        eval_prompt = (
            f"Source (English):\n{source}\n\n"
            f"Translation (French):\n{current_translation}"
        )
        eval_result = await Runner.run(evaluator_agent, eval_prompt)
        evaluation: EvaluationResult = eval_result.output

        print(
            f"\n[Evaluator] score={evaluation.score!r}  approved={evaluation.approved}"
        )
        if evaluation.issues:
            print("  Issues:", "; ".join(evaluation.issues))
        if evaluation.suggestions:
            print("  Suggestions:", "; ".join(evaluation.suggestions))

        if evaluation.approved:
            print("\n✓ Translation approved by the evaluator.")
            break

        feedback_context = (
            "Issues found:\n" + "\n".join(f"- {i}" for i in evaluation.issues) + "\n\n"
            "Suggestions:\n" + "\n".join(f"- {s}" for s in evaluation.suggestions)
        )
    else:
        print(f"\n⚠ Reached maximum iterations ({MAX_ITERATIONS}).")

    return current_translation


async def main() -> None:
    print("Source text:")
    print(SOURCE_TEXT)
    print("=" * 60)
    final = await optimize_translation(SOURCE_TEXT)
    print("\n--- Final Translation ---")
    print(final)


if __name__ == "__main__":
    asyncio.run(main())
