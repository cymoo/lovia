"""Parallelization workflow — Sectioning and Voting.

Demonstrates two variants of parallelization:

**Sectioning** — a single code-review task is split into three independent
  analyses (security, performance, style) that run concurrently, then the
  results are aggregated into a final report.

**Voting** — the same piece of content is evaluated by three independent
  judges with different framing prompts. A majority vote decides the final
  verdict, reducing the effect of any one model's bias.

Reference:
  https://www.anthropic.com/engineering/building-effective-agents#parallelization

Run::

    python examples/workflows/03_parallelization.py
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel

from lovia import Agent, Runner, model_from_env
from dotenv import load_dotenv

load_dotenv()

MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

# ---------------------------------------------------------------------------
# Sample code under review
# ---------------------------------------------------------------------------

SAMPLE_CODE = """\
import sqlite3

def get_user(username: str):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE username = '{username}'"
    cursor.execute(query)
    result = cursor.fetchone()
    conn.close()
    return result
"""


# ============================================================
# Part 1: SECTIONING
# ============================================================

security_reviewer = Agent(
    name="SecurityReviewer",
    instructions=(
        "You are an application-security expert. Review the code snippet for "
        "security vulnerabilities only (SQL injection, hardcoded credentials, "
        "improper input validation, etc.). Be specific and concise."
    ),
    model=MODEL,
)

performance_reviewer = Agent(
    name="PerformanceReviewer",
    instructions=(
        "You are a performance-optimization specialist. Review the code snippet "
        "for performance issues only (inefficient queries, connection pooling, "
        "unnecessary allocations, etc.). Be specific and concise."
    ),
    model=MODEL,
)

style_reviewer = Agent(
    name="StyleReviewer",
    instructions=(
        "You are a Python code-quality expert. Review the code snippet for style "
        "and maintainability issues only (PEP 8, naming, error handling, type "
        "hints, documentation). Be specific and concise."
    ),
    model=MODEL,
)

aggregator = Agent(
    name="ReviewAggregator",
    instructions=(
        "You are a lead engineer. Given individual reviews covering security, "
        "performance, and style, produce a single cohesive code-review summary "
        "with a prioritized list of recommended fixes."
    ),
    model=MODEL,
)


async def sectioning_example() -> None:
    print("=" * 60)
    print("SECTIONING — Parallel code review")
    print("=" * 60)
    print(f"Code:\n{SAMPLE_CODE}")

    prompt = f"Review this Python code:\n\n```python\n{SAMPLE_CODE}\n```"

    security_task = Runner.run(security_reviewer, prompt)
    performance_task = Runner.run(performance_reviewer, prompt)
    style_task = Runner.run(style_reviewer, prompt)

    sec, perf, style = await asyncio.gather(security_task, performance_task, style_task)

    print("\n--- Security Review ---")
    print(sec.output)
    print("\n--- Performance Review ---")
    print(perf.output)
    print("\n--- Style Review ---")
    print(style.output)

    aggregation_prompt = (
        f"Security Review:\n{sec.output}\n\n"
        f"Performance Review:\n{perf.output}\n\n"
        f"Style Review:\n{style.output}\n\n"
        "Produce a prioritized consolidated code-review report."
    )
    agg = await Runner.run(aggregator, aggregation_prompt)
    print("\n--- Aggregated Report ---")
    print(agg.output)


# ============================================================
# Part 2: VOTING
# ============================================================

CONTENT_TO_EVALUATE = """\
Act now! Limited-time offer — get 90% off our software suite TODAY ONLY!
Click here to claim your prize. You've been specially selected from millions!
"""


class ContentVerdict(BaseModel):
    is_spam: bool
    confidence: Literal["low", "medium", "high"]
    reason: str


judge_strict = Agent(
    name="StrictJudge",
    instructions=(
        "You are a strict anti-spam classifier. Err on the side of caution: "
        "if something looks remotely like spam or phishing, classify it as spam."
    ),
    model=MODEL,
    output_type=ContentVerdict,
)

judge_lenient = Agent(
    name="LenientJudge",
    instructions=(
        "You are a lenient content moderator. Only classify content as spam if "
        "it very clearly fits spam or phishing patterns. Legitimate marketing "
        "should not be flagged."
    ),
    model=MODEL,
    output_type=ContentVerdict,
)

judge_neutral = Agent(
    name="NeutralJudge",
    instructions=(
        "You are a balanced content classifier. Weigh both spam indicators and "
        "legitimate-content signals before deciding. Explain your reasoning."
    ),
    model=MODEL,
    output_type=ContentVerdict,
)


async def voting_example() -> None:
    print("\n" + "=" * 60)
    print("VOTING — Majority-vote spam detection")
    print("=" * 60)
    print(f"Content:\n{CONTENT_TO_EVALUATE}")

    prompt = f"Classify this content:\n\n{CONTENT_TO_EVALUATE}"

    strict_task = Runner.run(judge_strict, prompt)
    lenient_task = Runner.run(judge_lenient, prompt)
    neutral_task = Runner.run(judge_neutral, prompt)

    strict_r, lenient_r, neutral_r = await asyncio.gather(
        strict_task, lenient_task, neutral_task
    )

    verdicts: list[ContentVerdict] = [
        strict_r.output,
        lenient_r.output,
        neutral_r.output,
    ]
    judges = ["StrictJudge", "LenientJudge", "NeutralJudge"]

    print("\n--- Individual Verdicts ---")
    for name, v in zip(judges, verdicts):
        print(f"  {name}: is_spam={v.is_spam} [{v.confidence}] — {v.reason}")

    spam_votes = sum(1 for v in verdicts if v.is_spam)
    majority = spam_votes >= 2
    print(f"\n--- Majority Vote: is_spam={majority} ({spam_votes}/3 votes) ---")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    await sectioning_example()
    await voting_example()


if __name__ == "__main__":
    asyncio.run(main())
