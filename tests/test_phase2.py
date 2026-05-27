"""Tests for Phase-2 reliability primitives."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from lovia import (
    Agent,
    BudgetExceeded,
    CancelToken,
    ProviderError,
    RetryPolicy,
    RunBudget,
    RunCancelled,
    Runner,
    tool,
)
from lovia.messages import AssistantMessage, ChatMessage, Usage
from lovia.providers.base import ModelSettings, StreamChunk

from .scripted_provider import ScriptedProvider, call, text


def _heavy_response(tokens: int) -> AssistantMessage:
    return AssistantMessage(
        content="x" * tokens,
        usage=Usage(input_tokens=tokens, output_tokens=tokens),
    )


def test_budget_token_limit_raises() -> None:
    provider = ScriptedProvider([_heavy_response(50), _heavy_response(50)])
    agent = Agent(name="a", model=provider)
    budget = RunBudget(max_output_tokens=10)

    @tool
    async def noop() -> str:
        return "ok"

    agent.tools.append(noop)

    with pytest.raises(BudgetExceeded):
        asyncio.run(Runner.run(agent, "hi", budget=budget))


def test_budget_tool_call_limit() -> None:
    provider = ScriptedProvider(
        [call("noop", {}), call("noop", {}), call("noop", {}), text("done")]
    )

    @tool
    async def noop() -> str:
        return "ok"

    agent = Agent(name="a", model=provider, tools=[noop])
    budget = RunBudget(max_tool_calls=2)

    with pytest.raises(BudgetExceeded):
        asyncio.run(Runner.run(agent, "go", budget=budget))


def test_cancel_token_aborts_before_next_turn() -> None:
    token = CancelToken()

    @tool
    async def trip() -> str:
        token.cancel("user requested")
        return "ok"

    provider = ScriptedProvider([call("trip", {}), text("never reached")])
    agent = Agent(name="a", model=provider, tools=[trip])

    with pytest.raises(RunCancelled):
        asyncio.run(Runner.run(agent, "go", cancel_token=token))


class _FailingProvider:
    """Raises ``ProviderError`` for the first ``n`` attempts, then succeeds."""

    name = "failing"

    def __init__(self, fail_times: int, answer: AssistantMessage) -> None:
        self.fail_times = fail_times
        self.answer = answer
        self.attempts = 0

    async def generate(
        self, *a: Any, **kw: Any
    ) -> AssistantMessage:  # pragma: no cover
        raise NotImplementedError

    async def stream(self, *a: Any, **kw: Any) -> AsyncIterator[StreamChunk]:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise ProviderError(f"boom #{self.attempts}")
        yield StreamChunk(done=self.answer)


def test_retry_recovers_from_transient_error() -> None:
    provider = _FailingProvider(fail_times=2, answer=text("ok"))
    agent = Agent(name="a", model=provider)
    retry = RetryPolicy(
        max_attempts=5, backoff_base=0.0, sleep=lambda _d: asyncio.sleep(0)
    )

    result = asyncio.run(Runner.run(agent, "hi", retry=retry))
    assert result.output == "ok"
    assert provider.attempts == 3


def test_provider_fallback_chain() -> None:
    primary = _FailingProvider(fail_times=99, answer=text("never"))
    backup = ScriptedProvider([text("recovered")])
    agent = Agent(name="a", model=[primary, backup])
    retry = RetryPolicy(
        max_attempts=2, backoff_base=0.0, sleep=lambda _d: asyncio.sleep(0)
    )

    result = asyncio.run(Runner.run(agent, "hi", retry=retry))
    assert result.output == "recovered"


def test_anthropic_cache_control_inserted_when_cache_system_true() -> None:
    import httpx

    from lovia.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(
        model="claude-3-haiku-20240307",
        api_key="x",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ),
    )
    payload = provider._build_payload(
        messages=[
            ChatMessage(role="system", content="be terse"),
            ChatMessage(role="user", content="hi"),
        ],
        tools=[
            {
                "type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}},
            }
        ],
        settings=ModelSettings(cache_system=True),
        stream=False,
    )
    assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
