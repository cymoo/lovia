"""Opt-in live integration tests for configured provider endpoints."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from lovia import Agent, Runner
from lovia import events
from lovia.tools import tool

pytestmark = pytest.mark.live_provider


class TinyAnswer(BaseModel):
    answer: str


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


def _require_live() -> None:
    if os.getenv("LOVIA_LIVE_TESTS") != "1":
        pytest.skip("opt-in: set LOVIA_LIVE_TESTS=1 to run live provider tests")
    _load_env_file()


def _openai_chat_model() -> str:
    _require_live()
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not configured")
    return os.getenv("OPENAI_DEFAULT_MODEL", "gpt-5.4")


def _anthropic_model() -> str:
    _require_live()
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY is not configured")
    return os.getenv("ANTHROPIC_DEFAULT_MODEL", "claude-haiku-4-5")


@tool
def live_add(a: int, b: int) -> int:
    """Add two integers."""

    return a + b


@pytest.mark.asyncio
async def test_openai_chat_live_round_trip() -> None:
    model_name = _openai_chat_model()
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions="Answer in one short sentence.",
    )

    result = await Runner.run(agent, "Say hi.")

    assert isinstance(result.output, str)
    assert result.output
    assert result.usage.output_tokens > 0


@pytest.mark.asyncio
async def test_openai_chat_live_streaming() -> None:
    model_name = _openai_chat_model()
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions="Answer with exactly the word pong.",
    )

    chunks: list[str] = []
    async for event in Runner.stream(agent, "ping"):
        if isinstance(event, events.TextDelta):
            chunks.append(event.delta)

    assert "".join(chunks).strip()


@pytest.mark.asyncio
async def test_openai_chat_live_structured_output() -> None:
    model_name = _openai_chat_model()
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions="Return the requested structured answer.",
        output_type=TinyAnswer,
    )

    result = await Runner.run(agent, "Set answer to ok.")

    assert isinstance(result.output, TinyAnswer)
    assert result.output.answer


@pytest.mark.asyncio
async def test_openai_chat_live_tool_call() -> None:
    model_name = _openai_chat_model()
    agent = Agent(
        name="probe",
        model=f"openai:{model_name}",
        instructions="Use the live_add tool when arithmetic is requested.",
        tools=[live_add],
    )

    result = await Runner.run(agent, "Use the tool to add 2 and 3, then answer.")

    assert "5" in str(result.output)


@pytest.mark.asyncio
async def test_anthropic_live_round_trip() -> None:
    model_name = _anthropic_model()
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Answer in one short sentence.",
    )

    result = await Runner.run(agent, "Say hi.")

    assert isinstance(result.output, str)
    assert result.output
    assert result.usage.output_tokens > 0


@pytest.mark.asyncio
async def test_anthropic_live_structured_output() -> None:
    model_name = _anthropic_model()
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Return the requested structured answer.",
        output_type=TinyAnswer,
    )

    result = await Runner.run(agent, "Set answer to ok.")

    assert isinstance(result.output, TinyAnswer)
    assert result.output.answer


@pytest.mark.asyncio
async def test_anthropic_live_tool_call() -> None:
    model_name = _anthropic_model()
    agent = Agent(
        name="probe",
        model=f"anthropic:{model_name}",
        instructions="Use the live_add tool when arithmetic is requested.",
        tools=[live_add],
    )

    result = await Runner.run(agent, "Use the tool to add 2 and 3, then answer.")

    assert "5" in str(result.output)
