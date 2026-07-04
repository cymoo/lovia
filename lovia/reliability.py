"""Production-reliability primitives: budgets, retries, cancellation.

These types are small intentionally — they describe policies that the runner
honors at well-defined points (between turns, before a tool call, around a
provider stream). They are independent: each can be used without the others.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .exceptions import BudgetExceeded, ProviderError, RunCancelled
from .messages import Usage


@dataclass
class RunBudget:
    """Hard limits on a single run.

    The runner checks the budget *between* model turns and at each tool
    call's preflight (before it is dispatched). Tools and model calls that
    are already in flight when the budget trips are allowed to finish — under
    parallel tool execution a trip stops *dispatching* further calls and
    drains the in-flight ones to completion; the check happens at the next
    safe point.

    ``max_tool_calls`` counts every *requested* tool call — including ones the
    runner rejects (unknown tool, malformed arguments, denied approval), not
    just those that actually execute.

    Any limit set to ``None`` is unconstrained. ``max_seconds`` measures wall
    clock from the first :meth:`check` call.

    An instance carries single-run state (the wall-clock start, the tool-call
    count): create a fresh budget per run rather than reusing one across runs,
    or its clock and counters carry over. :func:`~lovia.handoff.agent_as_tool`
    copies the budget it was given per invocation for exactly this reason.
    """

    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_tool_calls: int | None = None
    max_seconds: float | None = None

    _started_at: float | None = field(default=None, init=False, repr=False)
    _tool_calls: int = field(default=0, init=False, repr=False)

    def record_tool_call(self) -> None:
        self._tool_calls += 1

    def check(self, usage: Usage) -> None:
        """Raise :class:`BudgetExceeded` if any limit is exceeded."""
        now = time.monotonic()
        if self._started_at is None:
            self._started_at = now

        if (
            self.max_input_tokens is not None
            and usage.input_tokens > self.max_input_tokens
        ):
            raise BudgetExceeded(
                f"input tokens {usage.input_tokens} exceeds budget {self.max_input_tokens}"
            )
        if (
            self.max_output_tokens is not None
            and usage.output_tokens > self.max_output_tokens
        ):
            raise BudgetExceeded(
                f"output tokens {usage.output_tokens} exceeds budget {self.max_output_tokens}"
            )
        if (
            self.max_total_tokens is not None
            and usage.total_tokens > self.max_total_tokens
        ):
            raise BudgetExceeded(
                f"total tokens {usage.total_tokens} exceeds budget {self.max_total_tokens}"
            )
        if self.max_tool_calls is not None and self._tool_calls > self.max_tool_calls:
            raise BudgetExceeded(
                f"tool call count {self._tool_calls} exceeds budget {self.max_tool_calls}"
            )
        if self.max_seconds is not None:
            elapsed = now - self._started_at
            if elapsed > self.max_seconds:
                raise BudgetExceeded(
                    f"elapsed {elapsed:.1f}s exceeds budget {self.max_seconds:.1f}s"
                )


@dataclass
class CancelToken:
    """A cooperative cancellation signal.

    Pass one to :meth:`Runner.run` and call :meth:`cancel` from any task to
    request termination. The runner checks the token between turns, at each
    tool call's preflight, and after each completed tool result — raising
    :class:`RunCancelled` at the next safe point (a mid-batch cancel also
    cancels the batch's still-running sibling calls).
    """

    _cancelled: bool = field(default=False, init=False)
    _reason: str | None = field(default=None, init=False)

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self, reason: str | None = None) -> None:
        self._cancelled = True
        self._reason = reason

    def check(self) -> None:
        if self._cancelled:
            msg = self._reason or "run cancelled"
            raise RunCancelled(msg)


# A predicate deciding whether an exception is worth retrying. Provider
# adapters mark deterministic failures as ``retryable=False``.
RetryPredicate = Callable[[BaseException], bool]


def _default_retry_on(exc: BaseException) -> bool:
    return isinstance(exc, ProviderError) and exc.retryable is not False


# Frozen: instances are shared (they are the default value of several
# ``retry=`` parameters), so the policy must stay immutable.
@dataclass(frozen=True)
class RetryPolicy:
    """Exponential-backoff retry policy applied around provider calls.

    The runner retries each call to :meth:`Provider.stream` according to this
    policy. By default (``restart_on_partial=True``) it also recovers from
    errors that strike **after** streaming has begun, using *replace*
    semantics: the partial output is discarded (the runner emits
    :class:`~lovia.events.OutputDiscarded`) and the turn is re-streamed from
    scratch. Streamed deltas are provisional until :class:`~lovia.events.MessageCompleted`;
    only then is the assistant turn durable. Set ``restart_on_partial=False``
    to keep the conservative behavior where a mid-stream error propagates
    immediately rather than re-streaming.

    ``retry_on`` decides which exceptions are transient. ``backoff_base`` and
    ``backoff_max`` define the exponential schedule; a small jitter is added
    so concurrent retries don't synchronize.

    ``max_attempts`` is the total number of calls to :meth:`Provider.stream`
    (the first call counts as attempt 1), so ``max_attempts=3`` means at most
    two retries and ``max_attempts=1`` disables retrying.

    If you supply :class:`Agent.model` as a list, the policy is applied
    *per-provider*; the runner moves to the next provider once retries on the
    current one are exhausted.
    """

    max_attempts: int = 3
    restart_on_partial: bool = True
    backoff_base: float = 0.5
    backoff_max: float = 8.0
    retry_on: RetryPredicate = field(default=_default_retry_on)
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)

    def backoff_delay(self, attempt: int) -> float:
        """Jittered exponential delay before retrying after ``attempt`` failures."""
        delay = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
        return float(delay * (0.5 + random.random()))
