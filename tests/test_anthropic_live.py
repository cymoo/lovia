"""Opt-in live integration test for the Anthropic provider.

Routes through the endpoint configured in ``.env`` — by default this points
at DeepSeek's Anthropic-compatible gateway. To run::

    LOVIA_LIVE_TESTS=1 uv run pytest tests/test_anthropic_live.py -v
"""

from __future__ import annotations

import os

import pytest

from lovia import Agent, Runner

pytestmark = pytest.mark.skipif(
    not os.getenv("LOVIA_LIVE_TESTS"),
    reason="opt-in: set LOVIA_LIVE_TESTS=1 to run",
)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv()


@pytest.mark.asyncio
async def test_anthropic_live_round_trip() -> None:
    _load_env()
    model_name = os.getenv("ANTHROPIC_DEFAULT_MODEL", "claude-3-5-haiku-latest")
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Answer in one short sentence.",
    )
    result = await Runner.run(agent, "Say hi.")
    assert isinstance(result.output, str)
    assert result.output
    assert result.usage.output_tokens > 0
