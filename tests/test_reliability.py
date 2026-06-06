"""Tests for reliability primitives: budgets, retries, fallback, cancellation."""

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
from lovia.transcript import (
    FinishDelta,
    ModelDelta,
    TextDelta,
    UsageDelta,
)
from lovia.messages import AssistantTurn, Usage
from lovia.reliability import RunBudget as _RunBudget  # noqa: F401  (re-export sanity)

from .scripted_provider import ScriptedProvider, call, text


def _heavy_response(tokens: int) -> AssistantTurn:
    return AssistantTurn(
        content="x" * tokens,
        usage=Usage(input_tokens=tokens, output_tokens=tokens),
    )


# ---------- RunBudget ----------


@pytest.mark.asyncio
async def test_budget_blocks_when_output_tokens_exceed_cap() -> None:
    provider = ScriptedProvider([_heavy_response(50)])
    agent = Agent(name="a", model=provider)
    with pytest.raises(BudgetExceeded):
        await Runner.run(agent, "hi", budget=RunBudget(max_output_tokens=10))


@pytest.mark.asyncio
async def test_budget_blocks_after_tool_call_cap() -> None:
    provider = ScriptedProvider(
        [call("noop", {}), call("noop", {}), call("noop", {}), text("done")]
    )

    @tool
    async def noop() -> str:
        return "ok"

    agent = Agent(name="a", model=provider, tools=[noop])
    with pytest.raises(BudgetExceeded):
        await Runner.run(agent, "go", budget=RunBudget(max_tool_calls=2))


@pytest.mark.asyncio
async def test_budget_below_cap_does_not_raise() -> None:
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="a", model=provider)
    result = await Runner.run(agent, "ping", budget=RunBudget(max_output_tokens=10_000))
    assert result.output == "hi"


@pytest.mark.asyncio
async def test_budget_check_is_noop_at_zero_usage() -> None:
    # Defensive: an empty Usage should never trip an unset budget.
    RunBudget().check(Usage())
    RunBudget(max_output_tokens=10).check(Usage())


# ---------- CancelToken ----------


@pytest.mark.asyncio
async def test_cancel_token_aborts_before_next_turn() -> None:
    token = CancelToken()

    @tool
    async def trip() -> str:
        token.cancel("user requested")
        return "ok"

    provider = ScriptedProvider([call("trip", {}), text("never reached")])
    agent = Agent(name="a", model=provider, tools=[trip])

    with pytest.raises(RunCancelled):
        await Runner.run(agent, "go", cancel_token=token)


def test_cancel_token_double_cancel_is_idempotent() -> None:
    token = CancelToken()
    token.cancel("first")
    token.cancel("second")  # must not raise
    with pytest.raises(RunCancelled):
        token.check()


def test_cancel_token_uncancelled_check_is_silent() -> None:
    CancelToken().check()  # no-op


# ---------- RetryPolicy + fallback chain ----------


class _FailingProvider:
    """Raises ``ProviderError`` for the first ``n`` attempts, then succeeds."""

    name = "failing"

    def __init__(
        self,
        fail_times: int,
        answer: AssistantTurn,
        *,
        retryable: bool | None = None,
    ) -> None:
        self.fail_times = fail_times
        self.answer = answer
        self.retryable = retryable
        self.attempts = 0

    async def stream(self, *a: Any, **kw: Any) -> AsyncIterator[ModelDelta]:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise ProviderError(f"boom #{self.attempts}", retryable=self.retryable)
        # Successful path: emit the canned answer as a minimal delta sequence.
        if self.answer.content:
            yield TextDelta(text=self.answer.content)
        yield UsageDelta(usage=self.answer.usage)
        yield FinishDelta(reason=self.answer.finish_reason)


@pytest.mark.asyncio
async def test_retry_recovers_from_transient_error() -> None:
    provider = _FailingProvider(fail_times=2, answer=text("ok"))
    agent = Agent(name="a", model=provider)
    retry = RetryPolicy(
        max_retries=5, backoff_base=0.0, sleep=lambda _d: asyncio.sleep(0)
    )

    result = await Runner.run(agent, "hi", retry=retry)
    assert result.output == "ok"
    assert provider.attempts == 3


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_attempts() -> None:
    provider = _FailingProvider(fail_times=99, answer=text("never"))
    agent = Agent(name="a", model=provider)
    retry = RetryPolicy(
        max_retries=2, backoff_base=0.0, sleep=lambda _d: asyncio.sleep(0)
    )
    with pytest.raises(ProviderError):
        await Runner.run(agent, "hi", retry=retry)
    assert provider.attempts == 2


@pytest.mark.asyncio
async def test_retry_does_not_retry_explicit_non_retryable_error() -> None:
    provider = _FailingProvider(fail_times=99, answer=text("never"), retryable=False)
    agent = Agent(name="a", model=provider)
    retry = RetryPolicy(
        max_retries=5, backoff_base=0.0, sleep=lambda _d: asyncio.sleep(0)
    )

    with pytest.raises(ProviderError):
        await Runner.run(agent, "hi", retry=retry)
    assert provider.attempts == 1


@pytest.mark.asyncio
async def test_provider_fallback_chain_uses_backup() -> None:
    primary = _FailingProvider(fail_times=99, answer=text("never"))
    backup = ScriptedProvider([text("recovered")])
    agent = Agent(name="a", model=[primary, backup])
    retry = RetryPolicy(
        max_retries=2, backoff_base=0.0, sleep=lambda _d: asyncio.sleep(0)
    )

    result = await Runner.run(agent, "hi", retry=retry)
    assert result.output == "recovered"
    assert primary.attempts == 2  # exhausted retries on primary then fell over
