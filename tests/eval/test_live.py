"""Opt-in live eval tests: set LOVIA_LIVE_TESTS=1 to run."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lovia import Agent
from lovia.eval import Case, contains, evaluate, llm_judge

pytestmark = pytest.mark.live_provider


def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _live_model() -> str:
    # Gate before loading: a normal run must not pull real .env keys into
    # os.environ (and the opt-in itself must come from the shell, not .env).
    if os.getenv("LOVIA_LIVE_TESTS") != "1":
        pytest.skip("opt-in: set LOVIA_LIVE_TESTS=1 to run live provider tests")
    _load_env_file()
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not configured")
    return os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.5")


async def test_live_evaluate_with_judge() -> None:
    model = _live_model()
    agent = Agent(
        name="geo",
        instructions="Answer concisely.",
        model=model,
    )
    report = await evaluate(
        agent,
        Case(
            "What is the capital of France?",
            checks=[
                contains("Paris"),
                llm_judge(
                    "The answer correctly and concisely names the capital.",
                    model=model,
                ),
            ],
        ),
    )
    print(f"\n{report}")
    assert report.passed, str(report)
    sample = report.cases[0].samples[0]
    assert sample.usage.total_tokens > 0
    assert sample.latency > 0
