"""Guardrails — pre-run input checks and post-run output checks.

A guardrail is just an async callable. Wrap one to gain a programmatic veto
over runs:

* **Input guardrails** receive the rendered initial transcript before the
  first model call. Use them to block obvious abuse, enforce length limits,
  PII redaction, etc.
* **Output guardrails** receive the run's final output. Use them to assert
  invariants the model can violate (e.g. "must mention a citation").

Any guardrail may signal a violation by raising :class:`GuardrailTripped`,
or by returning a truthy reason string. Returning ``None`` (or nothing)
indicates the value is acceptable.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Union

from .exceptions import GuardrailTripped
from .messages import Message
from .run_context import RunContext


GuardrailVerdict = Union[None, str, bool]
"""Falsy (``None``/``False``/``""``) means OK; a non-empty string or ``True``
triggers a violation."""


# Accept any callable, sync or async, returning a verdict or raising directly.
# Input guardrails are called as ``fn(messages, ctx)``, output guardrails as
# ``fn(output, ctx)`` — see the module docstring.
GuardrailFn = Callable[..., "GuardrailVerdict | Awaitable[GuardrailVerdict]"]


async def _run_guardrail(
    guard: GuardrailFn,
    arg: Any,
    ctx: RunContext[Any],
    *,
    kind: str,
) -> None:
    """Execute a guardrail; raise :class:`GuardrailTripped` on violation."""
    verdict = guard(arg, ctx)
    if inspect.isawaitable(verdict):
        verdict = await verdict
    if not verdict:
        return
    if verdict is True:
        raise GuardrailTripped(f"{kind} guardrail rejected the value")
    raise GuardrailTripped(f"{kind} guardrail: {verdict}")


async def check_input_guardrails(
    guardrails: list[GuardrailFn], messages: list[Message], ctx: RunContext[Any]
) -> None:
    for g in guardrails:
        await _run_guardrail(g, messages, ctx, kind="input")


async def check_output_guardrails(
    guardrails: list[GuardrailFn], output: Any, ctx: RunContext[Any]
) -> None:
    for g in guardrails:
        await _run_guardrail(g, output, ctx, kind="output")
