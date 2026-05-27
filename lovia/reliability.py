"""Production-reliability primitives: budgets, retries, cancellation.

These types are small intentionally — they describe policies that the runner
honors at well-defined points (between turns, before a tool call, around a
provider stream). They are independent: each can be used without the others.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .exceptions import BudgetExceeded, RunCancelled
from .messages import Usage


@dataclass
class RunBudget:
    """Hard limits on a single run.

    The runner checks the budget *between* model turns and *before* invoking a
    tool. Tools and model calls that are already in flight when the budget
    trips are allowed to finish; the check happens at the next safe point.

    Any limit set to ``None`` is unconstrained. ``max_seconds`` measures wall
    clock from the first :meth:`check` call.
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
        loop = asyncio.get_event_loop()
        now = loop.time()
        if self._started_at is None:
            self._started_at = now

        if self.max_input_tokens is not None and usage.input_tokens > self.max_input_tokens:
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
        if (
            self.max_tool_calls is not None
            and self._tool_calls > self.max_tool_calls
        ):
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
    request termination. The runner checks the token between turns and before
    each tool call, raising :class:`RunCancelled` at the next safe point.
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


# A predicate deciding whether an exception is worth retrying. Defaults to
# treating any ProviderError as transient.
RetryPredicate = Callable[[BaseException], bool]


def _default_retry_on(exc: BaseException) -> bool:
    from .exceptions import ProviderError

    return isinstance(exc, ProviderError)


@dataclass
class RetryPolicy:
    """Exponential-backoff retry policy applied around provider calls.

    The runner wraps every call to :meth:`Provider.stream` with this policy.
    ``retry_on`` decides which exceptions are transient. ``backoff_base`` and
    ``backoff_max`` define the exponential schedule; a small jitter is added
    so concurrent retries don't synchronize.

    If you supply :class:`Agent.model` as a list, the policy is applied
    *per-provider*; the runner moves to the next provider once retries on the
    current one are exhausted.
    """

    max_attempts: int = 3
    backoff_base: float = 0.5
    backoff_max: float = 8.0
    retry_on: RetryPredicate = field(default=_default_retry_on)
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)

    async def run(
        self, op: Callable[[], Awaitable[None]], *, on_error: Callable[[BaseException], None] | None = None
    ) -> None:
        """Run ``op`` with retries. ``op`` must be re-callable on failure."""
        attempt = 0
        while True:
            try:
                await op()
                return
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                attempt += 1
                if attempt >= self.max_attempts or not self.retry_on(exc):
                    raise
                if on_error is not None:
                    on_error(exc)
                delay = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
                delay *= 0.5 + random.random()
                await self.sleep(delay)
