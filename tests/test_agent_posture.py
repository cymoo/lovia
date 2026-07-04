"""Agent-level posture config: ``Agent.retry`` and ``Agent.context_policy``.

The placement rule under test: *posture* (retry, context policy) lives on the
agent and is inherited by every run; *limits* (max_turns, budget, cancel)
stay run-level. A per-run value always overrides the agent's.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from lovia import Agent, ProviderError, RetryPolicy, Runner
from lovia.context import Compaction, ContextPolicy
from lovia.context.policy import CompactionRequest, ContextResult
from lovia.providers.base import ModelSettings
from lovia.testing import ScriptedProvider, text
from lovia.transcript import FinishDelta, ModelDelta, TextDelta


class _FlakyProvider:
    """Fails on the first stream() call, succeeds on the second."""

    name = "flaky"
    model = "flaky-1"
    supports_json_schema = False

    def __init__(self) -> None:
        self.attempts = 0

    async def stream(
        self,
        entries: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ModelDelta]:
        self.attempts += 1
        if self.attempts == 1:
            raise ProviderError("transient boom", retryable=True)
        yield TextDelta(text="recovered")
        yield FinishDelta(reason="stop")


def _fast_retry(attempts: int) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=attempts, backoff_base=0.0, sleep=lambda _d: asyncio.sleep(0)
    )


async def test_agent_retry_is_inherited_by_runs() -> None:
    provider = _FlakyProvider()
    agent = Agent(name="a", model=provider, retry=_fast_retry(3))
    result = await Runner.run(agent, "hi")
    assert result.output == "recovered"
    assert provider.attempts == 2


async def test_agent_retry_none_disables_provider_retries() -> None:
    provider = _FlakyProvider()
    agent = Agent(name="a", model=provider, retry=None)
    with pytest.raises(ProviderError, match="transient boom"):
        await Runner.run(agent, "hi")
    assert provider.attempts == 1


async def test_run_retry_overrides_agent_posture() -> None:
    provider = _FlakyProvider()
    # Agent says "no retries"; the call opts back in for this run only.
    agent = Agent(name="a", model=provider, retry=None)
    result = await Runner.run(agent, "hi", retry=_fast_retry(3))
    assert result.output == "recovered"
    assert provider.attempts == 2


class _MarkerPolicy:
    """A trivial ContextPolicy that records whether it was consulted."""

    name = "marker"

    def __init__(self) -> None:
        self.calls = 0

    async def compact(self, req: CompactionRequest) -> ContextResult:
        self.calls += 1
        return ContextResult(entries=req.entries)


async def test_agent_context_policy_is_used() -> None:
    policy = _MarkerPolicy()
    agent = Agent(
        name="a", model=ScriptedProvider([text("ok")]), context_policy=policy
    )
    result = await Runner.run(agent, "hi")
    assert result.output == "ok"
    assert policy.calls >= 1


async def test_run_context_policy_overrides_agent() -> None:
    agent_policy = _MarkerPolicy()
    run_policy = _MarkerPolicy()
    agent = Agent(
        name="a", model=ScriptedProvider([text("ok")]), context_policy=agent_policy
    )
    await Runner.run(agent, "hi", context_policy=run_policy)
    assert run_policy.calls >= 1
    assert agent_policy.calls == 0


def test_agent_default_posture() -> None:
    agent = Agent(name="a")
    assert isinstance(agent.retry, RetryPolicy)  # retries on by default
    assert agent.context_policy is None  # falls back to the loop's Compaction


def test_context_policy_protocol_accepts_compaction() -> None:
    # Compaction satisfies the ContextPolicy protocol used by the Agent field.
    policy: ContextPolicy = Compaction(context_window=1000)
    agent = Agent(name="a", context_policy=policy)
    assert agent.context_policy is policy
